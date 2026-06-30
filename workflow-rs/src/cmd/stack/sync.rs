//! `wf stack sync` — push the in-scope branches to `origin`, and nothing else.
//!
//! Sync is intentionally the dumbest verb: it makes the remote branch tips match
//! the local ones. No forge, no MR. Keeping it that narrow is what lets the
//! other verbs assume the remote is current without entangling push failures
//! with MR logic.

use crate::core::git::Repository;

use super::{map_parallel, resolution};

pub fn run(repo: &Repository, all: bool) -> anyhow::Result<()> {
    let current = repo.current_branch();
    let plan = resolution::plan(repo, current.as_deref(), all)?;

    // Only push branches that actually exist locally; a name in the file with no
    // ref is a stale entry, not something to push.
    let tips = repo.branch_tips();
    let branches: Vec<String> = plan
        .selected
        .iter()
        .filter(|b| tips.contains_key(*b))
        .cloned()
        .collect();

    if branches.is_empty() {
        log::info!("nothing to push");
        return Ok(());
    }

    let results = map_parallel(&branches, |branch| {
        let outcome = repo.push_force_with_lease("origin", branch);
        (branch.clone(), outcome)
    });

    let mut failures = 0;
    for (branch, outcome) in results {
        match outcome {
            Ok(()) => log::info!("pushed {branch}"),
            Err(e) => {
                failures += 1;
                log::warn!("failed to push {branch}: {e}");
            }
        }
    }

    if failures > 0 {
        anyhow::bail!("{failures} branch(es) failed to push");
    }
    Ok(())
}
