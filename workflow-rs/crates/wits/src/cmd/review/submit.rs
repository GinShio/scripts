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
    let version = info.version;
    if version.head_sha.is_empty() {
        anyhow::bail!("MR {id} has no reviewed snapshot; run `wits review fetch {id}` for full detail");
    }

    if wits_log::is_dry_run() {
        preview(id, &local, forge.noun());
        return Ok(());
    }

    // The batched review: verdict + summary + line/file comments.
    let submission = build_submission(&local, &version);
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
        if keep_after_apply(forge, id, &action, review_ok) {
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
fn keep_after_apply(forge: &dyn Forge, id: &str, action: &Action, review_ok: bool) -> bool {
    match action {
        // A line/file comment rode the batch; keep it only if the batch failed.
        Action::Comment { file: Some(_), .. } => !review_ok,
        // An MR-level comment is its own call.
        Action::Comment { body, .. } => report_keep(id, "conversation comment", forge.comment_mr(id, body)),
        Action::Reply { thread, body } => report_keep(id, "reply", forge.reply(id, thread, body)),
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
fn build_submission(local: &Local, version: &wits_util::forge::DiffVersion) -> ReviewSubmission {
    let comments = local
        .actions
        .iter()
        .filter_map(|a| to_submit_comment(a, &version.head_sha))
        .collect();
    ReviewSubmission {
        verdict: local.verdict,
        summary: local.summary.clone(),
        comments,
        version: version.clone(),
    }
}

/// A line/file comment action → a submittable comment, anchored at the reviewed
/// head. MR-level comments and non-comment actions yield `None`.
fn to_submit_comment(action: &Action, head: &str) -> Option<SubmitComment> {
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
        body: body.clone(),
    })
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
