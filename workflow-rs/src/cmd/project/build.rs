//! `wf project build` — resolve a plan, honour the branch strategy, run the
//! backend's steps.
//!
//! Under the worktree strategy the target worktree must already exist (created
//! by `context create`); `build` never makes one implicitly. Under the in-place
//! strategy, building a non-current branch switches the focus's own-git repo to
//! it behind a [`RestoreGuard`], so the working tree is always returned to where
//! it started — even on failure.

use anyhow::{bail, Context, Result};

use crate::core::process::Command;

use super::backend::{self, EmitContext};
use super::git::{Git, RestoreGuard};
use super::model::{BranchStrategy, BuildMode, BuildOptions, Profile};
use super::resolve::{self, Plan, PlanInput};
use super::workspace::{ProjectData, Workspace};

pub fn run(
    ws: &Workspace,
    project: &ProjectData,
    profile: &Profile,
    opts: &BuildOptions,
) -> Result<()> {
    let focus = project.focus_name(profile.focus.as_deref()).to_owned();
    let identity = resolve::identity_repo(project, &focus);

    // The target branch: --branch, else the identity repo's current branch.
    let branch = match &profile.branch {
        Some(b) => b.clone(),
        None => current_branch(project, identity.as_deref())?,
    };

    let plan = make_plan(ws, project, profile, opts, &branch)?;

    let Some(build_dir) = plan.build_dir.clone() else {
        log::warn!(
            "project '{}': no build_dir configured — nothing to build",
            project.name
        );
        return Ok(());
    };
    let build_system = plan
        .build_system
        .clone()
        .context("build_dir is set but build_system is not")?;
    let be = backend::for_system(&build_system)
        .with_context(|| format!("unsupported build system '{build_system}'"))?;
    log::debug!("backend: {}", be.name());

    // Own-git repo the in-place dance acts on; kept alive for the build scope so
    // the restore guard can borrow it.
    let identity_git = match (plan.strategy, &plan.identity_repo) {
        (BranchStrategy::InPlace, Some(name)) => {
            Some(Git::new(project.repo_abs_path(name).with_context(
                || format!("cannot resolve path of repo '{name}'"),
            )?))
        }
        _ => None,
    };

    let _guard = match plan.strategy {
        BranchStrategy::Worktree => {
            if !plan.work_dir.exists() {
                bail!(
                    "worktree for branch '{}' does not exist at {} — run `project context create` first",
                    plan.branch_raw,
                    plan.work_dir.display()
                );
            }
            None
        }
        BranchStrategy::InPlace => match &identity_git {
            Some(git) => prepare_in_place(git, &plan)?,
            None => None,
        },
    };

    let steps = be.steps(&EmitContext {
        source_dir: &plan.work_dir,
        build_dir: &build_dir,
        install_dir: plan.install_dir.as_deref(),
        build_type: &plan.build_type,
        generator: plan.generator.as_deref(),
        target: opts.target.as_deref(),
        logical: &plan.logical,
        mode: opts.mode,
        install: opts.install,
    })?;

    for step in &steps {
        log::info!("{}", step.description);
        let mut cmd = Command::new(&step.program);
        cmd.args(step.args.iter().cloned()).current_dir(&step.cwd);
        for (k, v) in &plan.logical.environment {
            cmd.env(k, v);
        }
        let code = cmd.status()?;
        if code != 0 {
            bail!("{} failed (exit {code})", step.description);
        }
    }
    Ok(())
}

fn make_plan(
    ws: &Workspace,
    project: &ProjectData,
    profile: &Profile,
    opts: &BuildOptions,
    branch: &str,
) -> Result<Plan> {
    // Select-vs-inject (§5.3): in auto/build-only, an already-configured build
    // dir with no explicit toolchain request is trusted — skip injection so a
    // rerun does not reconfigure. Paths are unaffected, so we plan once to learn
    // the build dir, then re-plan without injection if we decide to trust it.
    let plan = plan_with(ws, project, profile, opts, branch, true)?;
    let explicit_toolchain =
        profile.toolchain.is_some() || std::env::var_os("WITS_PROJECT_TOOLCHAIN").is_some();
    let trust = matches!(opts.mode, BuildMode::Auto | BuildMode::BuildOnly)
        && !explicit_toolchain
        && plan
            .build_dir
            .as_ref()
            .zip(plan.build_system.as_deref().and_then(backend::for_system))
            .is_some_and(|(bd, be)| be.is_configured(bd));
    if trust {
        plan_with(ws, project, profile, opts, branch, false)
    } else {
        Ok(plan)
    }
}

fn plan_with(
    ws: &Workspace,
    project: &ProjectData,
    profile: &Profile,
    opts: &BuildOptions,
    branch: &str,
    inject_toolchain: bool,
) -> Result<Plan> {
    resolve::plan(
        ws,
        project,
        &PlanInput {
            profile,
            options: opts,
            branch,
            inject_toolchain,
        },
    )
}

/// Set up the in-place dance for the identity repo, returning a guard that
/// restores it. `None` when no switch is needed (already on the target).
fn prepare_in_place<'a>(git: &'a Git, plan: &Plan) -> Result<Option<RestoreGuard<'a>>> {
    let current = git
        .current_branch()
        .context("focus repo is in a detached HEAD; pass --branch and check out a branch")?;
    if current == plan.branch_raw {
        return Ok(None);
    }
    if !git.rev_exists(&plan.branch_raw) {
        bail!(
            "branch '{}' does not exist in the focus repo",
            plan.branch_raw
        );
    }

    let mut guard = RestoreGuard::capture(git);
    if git.stash_push("wf project auto-stash")? {
        guard.mark_stashed();
    }
    git.switch(&plan.branch_raw)?;
    // Align the focus repo's own submodules to the target's recorded state.
    let subs = git.materialised_submodules();
    git.submodule_update(&subs, false)?;
    Ok(Some(guard))
}

fn current_branch(project: &ProjectData, identity: Option<&str>) -> Result<String> {
    let name = identity.context("project has no own-git repo to take a branch from")?;
    let path = project
        .repo_abs_path(name)
        .with_context(|| format!("cannot resolve path of repo '{name}'"))?;
    Git::new(path)
        .current_branch()
        .context("detached HEAD is unsupported; pass --branch")
}
