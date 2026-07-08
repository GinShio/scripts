//! `wf project context {create,prune}` — manage a branch's build context,
//! strategy-transparently.
//!
//! A build context is a worktree (+ build dir) under the worktree strategy, or
//! just a build dir in-place. `create` materialises the worktree (a no-op
//! in-place); `prune` tears the whole thing down — worktree *and* build dir —
//! but never the install prefix, which may be shared. One command therefore
//! cleans up after a deleted branch regardless of the repo's strategy, which is
//! exactly what a git hook wants to call.

use std::path::Path;

use anyhow::{bail, Context, Result};

use crate::util::project::git::Git;
use crate::util::project::model::{BranchStrategy, Profile};
use crate::util::project::resolve::{self, Plan, PlanInput};
use crate::util::project::workspace::{ProjectData, Workspace};

pub fn create(
    ws: &Workspace,
    project: &ProjectData,
    profile: &Profile,
    branch: &str,
) -> Result<()> {
    let plan = plan_for(ws, project, profile, branch)?;
    if plan.strategy == BranchStrategy::InPlace {
        log::info!(
            "project '{}': in-place strategy — no worktree to create (build makes the build dir)",
            project.name
        );
        return Ok(());
    }

    let build_repo_path = project
        .repo_abs_path(&plan.build_repo)
        .with_context(|| format!("cannot resolve path of repo '{}'", plan.build_repo))?;
    let git = Git::new(&build_repo_path);
    let target = &plan.work_dir;

    if target.exists() {
        log::info!("worktree already exists at {}", target.display());
        return Ok(());
    }
    // git forbids one branch in two worktrees; report where it already lives.
    if let Some(existing) = git
        .worktrees()
        .into_iter()
        .find(|w| w.branch.as_deref() == Some(branch))
    {
        bail!(
            "branch '{branch}' is already checked out at {}",
            existing.path.display()
        );
    }
    if !git.rev_exists(branch) {
        bail!("branch '{branch}' does not exist — create it before making a worktree");
    }

    let sparse = git.is_sparse();
    git.worktree_add(target, branch, sparse)?;

    let wt = Git::new(target);
    if sparse {
        // Replicate the source's cone so the new worktree materialises only it,
        // then populate.
        let patterns = git.sparse_list();
        if !patterns.is_empty() {
            wt.sparse_set(&patterns)?;
        }
        wt.checkout_head()?;
    }
    // A fresh worktree is a fresh checkout, so submodules are initialised here
    // (cone-limited to what is materialised).
    wt.submodule_update(&wt.materialised_submodules(), true)?;
    log::info!("created worktree for '{branch}' at {}", target.display());
    Ok(())
}

pub fn prune(
    ws: &Workspace,
    project: &ProjectData,
    profile: &Profile,
    branch: &str,
    force: bool,
) -> Result<()> {
    let plan = plan_for(ws, project, profile, branch)?;

    if plan.strategy == BranchStrategy::Worktree && plan.work_dir.exists() {
        let build_repo_path = project
            .repo_abs_path(&plan.build_repo)
            .with_context(|| format!("cannot resolve path of repo '{}'", plan.build_repo))?;
        Git::new(&build_repo_path).worktree_remove(&plan.work_dir, force)?;
    }

    // Remove the build dir for this branch (both strategies); never install_dir.
    if let Some(build_dir) = &plan.build_dir {
        remove_dir(build_dir)?;
    }
    log::info!("pruned build context for '{branch}'");
    Ok(())
}

/// A paths-only plan (no toolchain injection needed) for a specific branch.
fn plan_for(
    ws: &Workspace,
    project: &ProjectData,
    profile: &Profile,
    branch: &str,
) -> Result<Plan> {
    resolve::plan(
        ws,
        project,
        &PlanInput {
            profile,
            branch,
            inject_toolchain: false,
            injector: None,
            extra_config_args: &[],
            extra_build_args: &[],
            extra_install_args: &[],
        },
    )
}

/// Remove a directory, honouring dry-run (print instead of delete).
fn remove_dir(dir: &Path) -> Result<()> {
    if !dir.exists() {
        return Ok(());
    }
    if crate::core::log::is_dry_run() {
        crate::core::log::dry_run(&format!("rm -rf {}", dir.display()));
        return Ok(());
    }
    std::fs::remove_dir_all(dir).with_context(|| format!("removing {}", dir.display()))?;
    Ok(())
}
