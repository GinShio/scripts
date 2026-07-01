//! `wf stack decorate` — add labels, assignees, and reviewers to MRs.
//!
//! Unlike the other verbs, attributes differ per MR (one branch wants one label,
//! its neighbour another), so this one is **single-MR by default**: it acts on
//! the named branch, or the current one. To set the *same* attributes across the
//! whole stack — a common `stacked` label, say — use `--all`. Per-branch
//! differences are expressed simply by running it once per branch with that
//! branch's flags (typically from a small per-repo script).
//!
//! It is additive and idempotent: it only adds what you list and never removes
//! anything, so a project's own label/reviewer automation is never clobbered, and
//! re-running is safe. Like `submit`, it leaves the work of *finding* the MR to
//! the forge and never pushes.

use crate::core::git::Repository;
use crate::core::log as wf_log;
use crate::util::forge::{self, Attributes, StateFilter};
use crate::util::remote::Remotes;

use super::{map_parallel, resolution, DecorateArgs};

enum Outcome {
    Done(String),
    NoMr,
    Failed(String),
}

pub fn run(repo: &Repository, args: &DecorateArgs) -> anyhow::Result<()> {
    let attrs = Attributes {
        labels: args.labels.clone(),
        assignees: args.assignees.clone(),
        reviewers: args.reviewers.clone(),
    };
    if attrs.is_empty() {
        anyhow::bail!("nothing to set: pass at least one --label / --assignee / --reviewer");
    }
    if args.all && args.branch.is_some() {
        anyhow::bail!("give a branch or --all, not both");
    }

    let branches = target_branches(repo, args)?;
    if branches.is_empty() {
        log::info!("no branches in scope");
        return Ok(());
    }

    let remotes = Remotes::resolve(repo);
    let forge = forge::detect(repo, &remotes)?;
    let noun = forge.noun();

    let outcomes = map_parallel(&branches, |branch| {
        let outcome = match forge.find(branch, StateFilter::Open) {
            Ok(Some(mr)) => {
                if wf_log::is_dry_run() {
                    wf_log::dry_run(&format!(
                        "decorate {noun} {} ({branch}): {}",
                        mr.display,
                        attrs.summary()
                    ));
                    Outcome::Done(mr.display)
                } else {
                    match forge.apply_attributes(&mr.id, &attrs) {
                        Ok(()) => Outcome::Done(mr.display),
                        Err(e) => Outcome::Failed(e.to_string()),
                    }
                }
            }
            Ok(None) => Outcome::NoMr,
            Err(e) => Outcome::Failed(e.to_string()),
        };
        (branch.clone(), outcome)
    });

    for (branch, outcome) in outcomes {
        match outcome {
            Outcome::Done(display) => log::info!("decorated {noun} {display} ({branch})"),
            Outcome::NoMr => log::info!("{branch}: no open {noun}"),
            Outcome::Failed(e) => log::warn!("{branch}: {e}"),
        }
    }
    Ok(())
}

/// One branch (the named one, or the current) by default; the whole in-scope
/// stack under `--all`.
fn target_branches(repo: &Repository, args: &DecorateArgs) -> anyhow::Result<Vec<String>> {
    if args.all {
        let current = repo.current_branch();
        return Ok(resolution::plan(repo, current.as_deref(), true)?.selected);
    }
    let branch = match &args.branch {
        Some(b) => b.clone(),
        None => repo
            .current_branch()
            .ok_or_else(|| anyhow::anyhow!("detached HEAD: name a branch to decorate"))?,
    };
    Ok(vec![branch])
}
