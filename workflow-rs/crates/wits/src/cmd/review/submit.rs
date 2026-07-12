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

use wits_util::forge::{
    Forge, LineRef, ReviewOutcome, ReviewSubmission, Side, SubmitComment, SubmitPlacement,
};
use wits_util::git::Repository;
use wits_util::log as wits_log;

use super::model::{bare_thread_id, Action, Local};
use super::{online, Online, SubmitArgs};

pub fn run(repo: &Repository, args: &SubmitArgs) -> Result<()> {
    let ctx = online(repo)?;

    let ids = target_ids(&ctx, args)?;
    if ids.is_empty() {
        log::info!("no drafts to submit");
        return Ok(());
    }

    // Submissions to different MRs are wholly independent — fan out over scoped
    // threads so network latency overlaps. Per-action reconciliation inside each
    // MR stays sequential (the store writes to distinct paths, so no races).
    let results = super::map_parallel(&ids, |id| {
        let result = submit_one(&ctx, id);
        (id.clone(), result)
    });

    let failures: Vec<_> = results
        .into_iter()
        .filter_map(|(id, r)| r.err().map(|e| (id, e)))
        .collect();
    if !failures.is_empty() {
        for (id, e) in &failures {
            log::warn!("MR {id}: {e}");
        }
        anyhow::bail!("{} MR(s) failed to submit", failures.len());
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

    let mut local = store.load_local(id)?;
    if local.is_empty() {
        log::info!("MR {id}: nothing to submit");
        return Ok(());
    }

    let info = store
        .load_info(id)
        .with_context(|| format!("MR {id} isn't fetched — run `wits review fetch {id}` first"))?;
    let version = info
        .current()
        .map(|s| s.version())
        .filter(|v| !v.head_sha.is_empty())
        .with_context(|| {
            format!(
                "MR {id} has no reviewed snapshot; run `wits review fetch {id}` for full detail"
            )
        })?;

    // Normalize: de-duplicate, collapse resolves, and stamp unstamped comments
    // with the current snapshot head so hand-edited drafts get anchored.
    local.normalize(&version.head_sha);

    if wits_log::is_dry_run() {
        preview(id, &local, forge.noun());
        return Ok(());
    }

    // The batched review: verdict + summary + line/file comments. Line-references
    // in bodies are expanded to forge permalinks against the reviewed head. The
    // forge reports a granular outcome per comment/summary/verdict so we
    // reconcile each action independently — a hard `Err` means nothing landed
    // (atomic backends, or a total failure) and the whole draft stays.
    let submission = build_submission(&local, &version, &info.snapshots, &info.files, forge);
    let outcome = if submission.is_empty() {
        ReviewOutcome::default()
    } else {
        match forge.submit_review(id, &submission) {
            Ok(o) => {
                if o.fully_ok() {
                    log::info!(
                        "MR {id}: posted review ({} comment(s))",
                        submission.comments.len()
                    );
                } else {
                    let failed = o.comment_results.iter().filter(|&&ok| !ok).count();
                    if failed > 0 {
                        log::warn!(
                            "MR {id}: {failed} of {} comment(s) failed",
                            o.comment_results.len()
                        );
                    }
                    if !o.summary_ok && local.summary.is_some() {
                        log::warn!("MR {id}: summary failed");
                    }
                    if o.verdict_ok == Some(false) {
                        log::warn!("MR {id}: verdict failed");
                    }
                }
                o
            }
            Err(e) => {
                log::warn!("MR {id}: review batch failed: {e}");
                // Nothing landed — synthesize an all-failed outcome so the
                // uniform per-action walk keeps every action in the draft.
                ReviewOutcome {
                    comment_results: vec![false; submission.comments.len()],
                    summary_ok: false,
                    verdict_ok: local.verdict.is_some().then_some(false),
                }
            }
        }
    };

    // The independent actions; keep any that fail. Line/file comments are
    // matched to `outcome.comment_results` in order.
    let mut survivors: Vec<Action> = Vec::new();
    let mut comment_idx = 0usize;
    for action in local.actions.drain(..) {
        if keep_after_apply(
            forge,
            id,
            &action,
            &outcome,
            &mut comment_idx,
            &version.head_sha,
        ) {
            survivors.push(action);
        }
    }

    local.actions = survivors;
    if outcome.summary_ok {
        local.summary = None;
    }
    if outcome.verdict_ok == Some(true) {
        local.verdict = None;
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
/// it rode the review batch and that batch reports it failed).
///
/// `comment_idx` tracks the position in `outcome.comment_results` across calls,
/// so each line/file comment is matched to its per-comment result in order.
fn keep_after_apply(
    forge: &dyn Forge,
    id: &str,
    action: &Action,
    outcome: &ReviewOutcome,
    comment_idx: &mut usize,
    default_ref: &str,
) -> bool {
    match action {
        // A line/file comment rode the batch — its result is at the next index.
        Action::Comment { file: Some(_), .. } => {
            let ok = outcome
                .comment_results
                .get(*comment_idx)
                .copied()
                .unwrap_or(false);
            *comment_idx += 1;
            !ok
        }
        // An MR-level comment is its own call. Its `[[…]]` references resolve against
        // the action's own commit (the snapshot the comment was written on) when
        // set, else the current snapshot head — consistent with line/file
        // comments, which anchor to the action's version.
        Action::Comment { body, commit, .. } => {
            let default_ref = commit.as_deref().unwrap_or(default_ref);
            let body = expand_refs(body, forge, default_ref);
            report_keep(id, "conversation comment", forge.comment_mr(id, &body))
        }
        Action::Reply { thread, body } => {
            let body = expand_refs(body, forge, default_ref);
            report_keep(id, "reply", forge.reply(id, bare_thread_id(thread), &body))
        }
        Action::Resolve { thread, resolved } => report_keep(
            id,
            "resolve",
            forge.resolve(id, bare_thread_id(thread), *resolved),
        ),
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
/// Bodies have their `[[…]]` references expanded to forge permalinks. Each
/// comment is anchored to the [`DiffVersion`] of the snapshot it was written on
/// — resolved from `snapshots` by the comment's own `commit` (the snapshot head
/// recorded at ingest), falling back to the current version.
fn build_submission(
    local: &Local,
    version: &wits_util::forge::DiffVersion,
    snapshots: &[super::model::Snapshot],
    files: &[super::model::StoredFile],
    forge: &dyn Forge,
) -> ReviewSubmission {
    let comments = local
        .actions
        .iter()
        .filter_map(|a| to_submit_comment(a, version, snapshots, files, forge))
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

/// Resolve the per-snapshot [`DiffVersion`] a comment was written against. The
/// comment's `commit` is a snapshot head SHA (stamped at ingest); look it up in
/// the history so GitLab's `position` gets the right `base`/`start`/`head`
/// triple. An unset or un-found `commit` falls back to the current version.
fn comment_version(
    action_commit: Option<&str>,
    snapshots: &[super::model::Snapshot],
    version: &wits_util::forge::DiffVersion,
) -> wits_util::forge::DiffVersion {
    if let Some(sha) = action_commit {
        if let Some(s) = snapshots.iter().rev().find(|s| s.head_sha == sha) {
            return s.version();
        }
    }
    version.clone()
}

/// Look up a changed file's pre-image path (`old_path`) by its new path. Renames
/// and copies carry `old_path`; for any other status it is `None`. Used to carry
/// the pre-image path through to the forge anchor (GitLab needs it for a comment
/// on a renamed/deleted file's old side).
fn old_path_for(path: &str, files: &[super::model::StoredFile]) -> Option<String> {
    files
        .iter()
        .find(|f| f.path == path)
        .and_then(|f| f.old_path.clone())
}

/// A line/file comment action → a submittable comment, anchored at the version
/// of the snapshot it was written against. MR-level comments and non-comment
/// actions yield `None`.
fn to_submit_comment(
    action: &Action,
    version: &wits_util::forge::DiffVersion,
    snapshots: &[super::model::Snapshot],
    files: &[super::model::StoredFile],
    forge: &dyn Forge,
) -> Option<SubmitComment> {
    let Action::Comment {
        file: Some(path),
        line,
        side,
        start_line,
        start_side,
        body,
        commit: action_commit,
    } = action
    else {
        return None;
    };
    let cv = comment_version(action_commit.as_deref(), snapshots, version);
    let old_path = old_path_for(path, files);
    let placement = match line {
        Some(line) => {
            let s = side.unwrap_or(Side::New);
            let end = LineRef {
                line: *line,
                side: s,
            };
            let start = start_line.map(|sl| LineRef {
                line: sl,
                side: start_side.unwrap_or(s),
            });
            SubmitPlacement::Line {
                path: path.clone(),
                old_path,
                end,
                start,
                version: cv.clone(),
            }
        }
        None => SubmitPlacement::File {
            path: path.clone(),
            version: cv.clone(),
        },
    };
    Some(SubmitComment {
        placement,
        body: expand_refs(body, forge, &cv.head_sha),
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
        wits_log::dry_run(&format!("submit {noun} {id}: verdict {}", v.display_str()));
    }
    for action in &local.actions {
        let line = match action {
            Action::Comment {
                file: Some(f),
                line: Some(l),
                ..
            } => format!("comment on {f}:{l}"),
            Action::Comment { file: Some(f), .. } => format!("comment on file {f}"),
            Action::Comment { .. } => "conversation comment".to_owned(),
            Action::Reply { thread, .. } => format!("reply to {}", bare_thread_id(thread)),
            Action::Resolve { thread, resolved } => {
                let verb = if *resolved { "resolve" } else { "unresolve" };
                format!("{verb} {}", bare_thread_id(thread))
            }
        };
        wits_log::dry_run(&format!("submit {noun} {id}: {line}"));
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
        assert_eq!(
            go("[[README.md]]"),
            "https://github.com/o/r/blob/deadbeef/README.md"
        );
        // `@ref` pins another commit/branch.
        assert_eq!(
            go("[[src/y.c:9@main]]"),
            "https://github.com/o/r/blob/main/src/y.c#L9"
        );
        // A non-reference is untouched; an unterminated token is left as written.
        assert_eq!(go("no refs here"), "no refs here");
        assert_eq!(go("dangling [[oops"), "dangling [[oops");
    }
}
