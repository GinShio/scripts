//! `wits review submit` — flush the local draft to the forge.
//!
//! The one network write. It reads `local.json`, merges and de-duplicates the
//! recorded actions, and posts them. The verdict, summary, and line/file
//! comments go up as one batched review (one notification where the platform
//! allows); MR-level conversation comments, replies, and resolves are separate
//! calls. Reconciliation is **per action**: whatever lands is cleared, whatever
//! fails stays in the draft to retry. Only a fully-flushed draft triggers a
//! re-fetch, so a partial failure never loses unposted work.

use anyhow::{Context, Result};

use wits_util::forge::{Forge, ReviewSubmission, SubmitComment, SubmitPlacement};
use wits_util::git::Repository;
use wits_util::log as wits_log;

use super::model::{Action, Local};
use super::{online, Online, SubmitArgs};

pub fn run(repo: &Repository, args: &SubmitArgs) -> Result<()> {
    let ctx = online(repo)?;

    let ids = target_ids(&ctx, args)?;
    if ids.is_empty() {
        log::info!("no drafts to submit");
        return Ok(());
    }

    let mut failures = 0;
    for id in &ids {
        if let Err(e) = submit_one(&ctx, id) {
            failures += 1;
            log::warn!("MR {id}: {e}");
        }
    }
    if failures > 0 {
        anyhow::bail!("{failures} MR(s) failed to submit");
    }
    Ok(())
}

fn target_ids(ctx: &Online, args: &SubmitArgs) -> Result<Vec<String>> {
    if args.all {
        return Ok(ctx.local.store.local_ids());
    }
    let handle = args.mr.as_ref().context("give an MR to submit, or --all")?;
    let id = super::parse_mr_handle(handle)?;

    if args.stack {
        let infos = ctx.local.store.list_infos();
        let (chain, _) = super::stack_chain(&infos, &id);
        let drafted: std::collections::HashSet<String> =
            ctx.local.store.local_ids().into_iter().collect();
        Ok(chain.into_iter().filter(|n| drafted.contains(n)).collect())
    } else {
        Ok(vec![id])
    }
}

fn submit_one(ctx: &Online, id: &str) -> Result<()> {
    let store = &ctx.local.store;
    let forge = ctx.forge.as_ref();

    let mut local = store.load_local(id);
    if local.is_empty() {
        log::info!("MR {id}: nothing to submit");
        return Ok(());
    }
    local.normalize();

    let info = store
        .load_info(id)
        .with_context(|| format!("MR {id} isn't fetched — run `wits review fetch {id}` first"))?;
    let version = info
        .current()
        .map(|s| s.version())
        .filter(|v| !v.head_sha.is_empty())
        .with_context(|| {
            format!("MR {id} has no reviewed snapshot; run `wits review fetch {id}` for full detail")
        })?;

    if wits_log::is_dry_run() {
        preview(id, &local, forge.noun());
        return Ok(());
    }

    // The batched review: verdict + summary + line/file comments. Line-references
    // in bodies are expanded to forge permalinks against the reviewed head.
    let submission = build_submission(&local, &version, forge);
    let review_ok = if submission.is_empty() {
        true
    } else {
        match forge.submit_review(id, &submission) {
            Ok(()) => {
                log::info!("MR {id}: posted review ({} comment(s))", submission.comments.len());
                true
            }
            Err(e) => {
                log::warn!("MR {id}: review batch failed: {e}");
                false
            }
        }
    };

    // The independent actions; keep any that fail.
    let mut survivors: Vec<Action> = Vec::new();
    for action in local.actions.drain(..) {
        if keep_after_apply(forge, id, &action, review_ok, &version.head_sha) {
            survivors.push(action);
        }
    }

    local.actions = survivors;
    if review_ok {
        local.verdict = None;
        local.summary = None;
    }
    store.save_local(id, &local)?;

    if local.is_empty() {
        if let Err(e) = super::fetch::refresh(ctx, id) {
            log::warn!("MR {id}: submitted, but refreshing the cache failed: {e}");
        }
        log::info!("MR {id}: submitted");
        Ok(())
    } else {
        anyhow::bail!("some actions did not submit; they remain in the draft")
    }
}

/// Apply one action, returning whether it must stay in the draft (it failed, or
/// it rode the review batch and that batch failed).
fn keep_after_apply(
    forge: &dyn Forge,
    id: &str,
    action: &Action,
    review_ok: bool,
    default_ref: &str,
) -> bool {
    match action {
        // A line/file comment rode the batch; keep it only if the batch failed.
        Action::Comment { file: Some(_), .. } => !review_ok,
        // An MR-level comment is its own call.
        Action::Comment { body, .. } => {
            let body = expand_refs(body, forge, default_ref);
            report_keep(id, "conversation comment", forge.comment_mr(id, &body))
        }
        Action::Reply { thread, body } => {
            let body = expand_refs(body, forge, default_ref);
            report_keep(id, "reply", forge.reply(id, thread, &body))
        }
        Action::Resolve { thread, resolved } => {
            report_keep(id, "resolve", forge.resolve(id, thread, *resolved))
        }
    }
}

fn report_keep(id: &str, what: &str, result: Result<()>) -> bool {
    match result {
        Ok(()) => {
            log::info!("MR {id}: {what} posted");
            false
        }
        Err(e) => {
            log::warn!("MR {id}: {what} failed: {e}");
            true
        }
    }
}

/// Build the batched review from the verdict, summary, and line/file comments.
/// MR-level comments, replies, and resolves are excluded (separate calls).
/// Bodies have their `[[…]]` references expanded to forge permalinks.
fn build_submission(
    local: &Local,
    version: &wits_util::forge::DiffVersion,
    forge: &dyn Forge,
) -> ReviewSubmission {
    let comments = local
        .actions
        .iter()
        .filter_map(|a| to_submit_comment(a, version, forge))
        .collect();
    ReviewSubmission {
        verdict: local.verdict,
        summary: local
            .summary
            .as_ref()
            .map(|s| expand_refs(s, forge, &version.head_sha)),
        comments,
        version: version.clone(),
    }
}

/// A line/file comment action → a submittable comment, anchored at the reviewed
/// head. MR-level comments and non-comment actions yield `None`.
fn to_submit_comment(
    action: &Action,
    version: &wits_util::forge::DiffVersion,
    forge: &dyn Forge,
) -> Option<SubmitComment> {
    let Action::Comment {
        file: Some(path),
        line,
        side,
        start_line,
        body,
    } = action
    else {
        return None;
    };
    let head = &version.head_sha;
    let placement = match line {
        Some(line) => SubmitPlacement::Line {
            path: path.clone(),
            old_path: None,
            side: side.unwrap_or(wits_util::forge::Side::New),
            line: *line,
            start_line: *start_line,
            commit: head.to_owned(),
        },
        None => SubmitPlacement::File {
            path: path.clone(),
            commit: head.to_owned(),
        },
    };
    Some(SubmitComment {
        placement,
        body: expand_refs(body, forge, head),
    })
}

/// Expand every `[[path:line]]` reference in `body` to a forge permalink. The
/// grammar: `path` (repo-relative), optional `:line` or `:start-end`, optional
/// `@ref` to pin a commit/branch/tag (default: the reviewed head). Unterminated
/// or unparseable tokens are left as written.
fn expand_refs(body: &str, forge: &dyn Forge, default_ref: &str) -> String {
    let mut out = String::with_capacity(body.len());
    let mut rest = body;
    while let Some(start) = rest.find("[[") {
        out.push_str(&rest[..start]);
        let after = &rest[start + 2..];
        match after.find("]]") {
            Some(end) => {
                out.push_str(&expand_one(after[..end].trim(), forge, default_ref));
                rest = &after[end + 2..];
            }
            None => {
                out.push_str("[[");
                rest = after;
            }
        }
    }
    out.push_str(rest);
    out
}

fn expand_one(token: &str, forge: &dyn Forge, default_ref: &str) -> String {
    let (locpart, r#ref) = match token.rsplit_once('@') {
        Some((l, r)) if !r.is_empty() => (l, r),
        _ => (token, default_ref),
    };
    let (path, lines) = match locpart.rsplit_once(':') {
        Some((p, spec)) => match parse_lines(spec) {
            Some(lines) => (p, Some(lines)),
            None => (locpart, None),
        },
        None => (locpart, None),
    };
    forge.permalink(r#ref, path, lines)
}

/// Parse a `N` or `N-M` line spec.
fn parse_lines(spec: &str) -> Option<(u32, Option<u32>)> {
    match spec.split_once('-') {
        Some((a, b)) => Some((a.parse().ok()?, Some(b.parse().ok()?))),
        None => Some((spec.parse().ok()?, None)),
    }
}

/// Print what a submit would do, without touching the forge.
fn preview(id: &str, local: &Local, noun: &str) {
    if let Some(v) = local.verdict {
        wits_log::dry_run(&format!("submit {noun} {id}: verdict {}", verdict_word(v)));
    }
    for action in &local.actions {
        let line = match action {
            Action::Comment { file: Some(f), line: Some(l), .. } => format!("comment on {f}:{l}"),
            Action::Comment { file: Some(f), .. } => format!("comment on file {f}"),
            Action::Comment { .. } => "conversation comment".to_owned(),
            Action::Reply { thread, .. } => format!("reply to {thread}"),
            Action::Resolve { thread, resolved } => {
                let verb = if *resolved { "resolve" } else { "unresolve" };
                format!("{verb} {thread}")
            }
        };
        wits_log::dry_run(&format!("submit {noun} {id}: {line}"));
    }
}

fn verdict_word(v: wits_util::forge::Verdict) -> &'static str {
    match v {
        wits_util::forge::Verdict::Approve => "approve",
        wits_util::forge::Verdict::RequestChanges => "request-changes",
        wits_util::forge::Verdict::Comment => "comment",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use wits_util::forge::github::GitHub;
    use wits_util::remote::{RemoteInfo, Service};

    fn github() -> GitHub {
        GitHub::new(
            RemoteInfo {
                host: "github.com".into(),
                owner: "o".into(),
                repo: "r".into(),
                service: Service::GitHub,
            },
            None,
            "t".into(),
            None,
        )
    }

    #[test]
    fn expands_line_references_to_permalinks() {
        let gh = github();
        let go = |b: &str| expand_refs(b, &gh, "deadbeef");

        assert_eq!(
            go("see [[src/y.c:20]] here"),
            "see https://github.com/o/r/blob/deadbeef/src/y.c#L20 here"
        );
        assert_eq!(
            go("[[src/y.c:20-25]]"),
            "https://github.com/o/r/blob/deadbeef/src/y.c#L20-L25"
        );
        // A whole-file reference has no line fragment.
        assert_eq!(go("[[README.md]]"), "https://github.com/o/r/blob/deadbeef/README.md");
        // `@ref` pins another commit/branch.
        assert_eq!(go("[[src/y.c:9@main]]"), "https://github.com/o/r/blob/main/src/y.c#L9");
        // A non-reference is untouched; an unterminated token is left as written.
        assert_eq!(go("no refs here"), "no refs here");
        assert_eq!(go("dangling [[oops"), "dangling [[oops");
    }
}
