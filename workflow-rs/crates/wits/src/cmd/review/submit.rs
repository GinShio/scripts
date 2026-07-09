//! `wits review submit` — flush pending drafts to the forge.
//!
//! The one network write. A single MR's draft can expand to several forge calls
//! (the batched review, MR-level conversation comments, replies, resolves), so
//! reconciliation is **per action**: each that lands is cleared, each that fails
//! stays in the draft to retry. Only when a draft empties completely do we
//! re-fetch, so the just-posted comments come back as ordinary remote threads.

use anyhow::{Context, Result};

use wits_util::forge::{Forge, ReviewSubmission, SubmitComment, SubmitPlacement};
use wits_util::git::Repository;
use wits_util::log as wits_log;

use super::model::{Action, Draft, Placement};
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

/// Which MRs to submit: one, a whole stack, or every drafted MR.
fn target_ids(ctx: &Online, args: &SubmitArgs) -> Result<Vec<String>> {
    if args.all {
        return Ok(ctx.local.store.draft_ids());
    }
    let handle = args.mr.as_ref().context("give an MR to submit, or --all")?;
    let id = super::parse_mr_handle(handle)?;

    if args.stack {
        // Every node of the stack around this MR that has something to submit.
        let caches = ctx.local.store.list_cached();
        let (chain, _) = super::stack_chain(&caches, &id);
        let drafted: std::collections::HashSet<String> =
            ctx.local.store.draft_ids().into_iter().collect();
        Ok(chain.into_iter().filter(|n| drafted.contains(n)).collect())
    } else {
        Ok(vec![id])
    }
}

fn submit_one(ctx: &Online, id: &str) -> Result<()> {
    let store = &ctx.local.store;
    let forge = ctx.forge.as_ref();
    let mut draft = store.load_draft(id);
    if draft.is_empty() {
        log::info!("MR {id}: nothing to submit");
        return Ok(());
    }

    let cache = store
        .load_cache(id)
        .with_context(|| format!("MR {id} isn't fetched — run `wits review fetch {id}` first"))?;
    let head = cache.version.head_sha.clone();

    // Warn when a code anchor was written against a different snapshot than the
    // one we now hold: its line numbers may not line up (a v1 limitation).
    for action in &draft.actions {
        if let Action::Comment {
            placement: Placement::Line { commit, .. } | Placement::File { commit, .. },
            ..
        } = action
        {
            if let Some(c) = commit {
                if !head.is_empty() && *c != head {
                    log::warn!(
                        "MR {id}: a comment was written against {} but the snapshot is now {}; \
                         anchors may be off",
                        &c[..c.len().min(8)],
                        &head[..head.len().min(8)]
                    );
                    break;
                }
            }
        }
    }

    if wits_log::is_dry_run() {
        preview(id, &draft, forge.noun());
        return Ok(());
    }

    // Split into the batched review (verdict + summary + line/file comments) and
    // the independent actions (mr comments, replies, resolves).
    let submission = build_submission(&draft, &cache.version);
    let mut survivors: Vec<Action> = Vec::new();
    let mut review_ok = true;

    if !submission.is_empty() {
        match forge.submit_review(id, &submission) {
            Ok(()) => log::info!(
                "MR {id}: posted review ({} comment(s))",
                submission.comments.len()
            ),
            Err(e) => {
                review_ok = false;
                log::warn!("MR {id}: review batch failed: {e}");
            }
        }
    }

    for action in draft.actions.drain(..) {
        let kept = apply_action(forge, id, &action, review_ok);
        if kept {
            survivors.push(action);
        }
    }

    // Rebuild the draft from what didn't land; clear the verdict/summary only if
    // the review batch that carried them succeeded.
    draft.actions = survivors;
    if review_ok {
        draft.verdict = None;
        draft.summary = None;
    }
    store.save_draft(id, &draft)?;

    // A fully-flushed draft: refresh the cache so the new threads come back.
    if draft.is_empty() {
        if let Err(e) = super::fetch::refresh(ctx, id) {
            log::warn!("MR {id}: submitted, but refreshing the cache failed: {e}");
        }
        log::info!("MR {id}: submitted");
    } else {
        anyhow::bail!("some actions did not submit; they remain in the draft");
    }
    Ok(())
}

/// Apply one non-batched action, returning whether it should be kept (i.e. it
/// failed or was folded into the review batch and that batch failed).
fn apply_action(forge: &dyn Forge, id: &str, action: &Action, review_ok: bool) -> bool {
    match action {
        // Line/file comments rode the review batch; keep them only if it failed.
        Action::Comment {
            placement: Placement::Line { .. } | Placement::File { .. },
            ..
        } => !review_ok,
        Action::Comment {
            placement: Placement::Mr,
            body,
            ..
        } => report_keep(id, "conversation comment", forge.comment_mr(id, body)),
        Action::Reply { thread, body, .. } => {
            report_keep(id, "reply", forge.reply(id, thread, body))
        }
        Action::Resolve { thread, resolved } => {
            report_keep(id, "resolve", forge.resolve(id, thread, *resolved))
        }
    }
}

/// Log an individual action's outcome; keep it in the draft only on failure.
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

/// Build the batched review from a draft's verdict, summary, and line/file
/// comments. MR-level comments, replies, and resolves are excluded (they are
/// separate calls).
fn build_submission(draft: &Draft, version: &wits_util::forge::DiffVersion) -> ReviewSubmission {
    let comments = draft
        .actions
        .iter()
        .filter_map(|a| match a {
            Action::Comment {
                placement, body, ..
            } => to_submit_placement(placement, &version.head_sha).map(|placement| SubmitComment {
                placement,
                body: body.clone(),
            }),
            _ => None,
        })
        .collect();
    ReviewSubmission {
        verdict: draft.verdict,
        summary: draft.summary.clone(),
        comments,
        version: version.clone(),
    }
}

fn to_submit_placement(placement: &Placement, head: &str) -> Option<SubmitPlacement> {
    match placement {
        Placement::Line {
            path,
            old_path,
            side,
            line,
            start_line,
            commit,
        } => Some(SubmitPlacement::Line {
            path: path.clone(),
            old_path: old_path.clone(),
            side: *side,
            line: *line,
            start_line: *start_line,
            commit: commit.clone().unwrap_or_else(|| head.to_owned()),
        }),
        Placement::File { path, commit } => Some(SubmitPlacement::File {
            path: path.clone(),
            commit: commit.clone().unwrap_or_else(|| head.to_owned()),
        }),
        Placement::Mr => None,
    }
}

/// Print what a submit would do, without touching the forge.
fn preview(id: &str, draft: &Draft, noun: &str) {
    if let Some(v) = draft.verdict {
        let word = match v {
            wits_util::forge::Verdict::Approve => "approve",
            wits_util::forge::Verdict::RequestChanges => "request-changes",
            wits_util::forge::Verdict::Comment => "comment",
        };
        wits_log::dry_run(&format!("submit {noun} {id}: verdict {word}"));
    }
    for action in &draft.actions {
        let line = match action {
            Action::Comment { placement, .. } => match placement {
                Placement::Line { path, line, .. } => format!("comment on {path}:{line}"),
                Placement::File { path, .. } => format!("comment on file {path}"),
                Placement::Mr => "conversation comment".to_owned(),
            },
            Action::Reply { thread, .. } => format!("reply to remote:{thread}"),
            Action::Resolve { thread, resolved } => {
                let verb = if *resolved { "resolve" } else { "unresolve" };
                format!("{verb} remote:{thread}")
            }
        };
        wits_log::dry_run(&format!("submit {noun} {id}: {line}"));
    }
}
