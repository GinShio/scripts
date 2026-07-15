//! `wits build` — resolve a plan, honour the branch strategy, run the backend's
//! steps.
//!
//! Its own top-level command (§1.3 of `docs/project/design.md`), but entirely
//! built on `project`'s read-only core: `project` is the component that knows
//! what a project *is*; this module only knows how to *build* one.
//!
//! The build systems live in [`wits_util::build_system`], beside the
//! read-only core they build on: they are purely a build-time concern, so
//! `project` need not expose them (§1.4). The one thing the core resolver still
//! needs — translating a toolchain into native env/definitions at L0 (§5.4) —
//! it gets through the `ToolchainInjector` seam, which each backend implements
//! and `build` hands to `resolve::plan`.
//!
//! Under the worktree strategy the target worktree must already exist (created
//! by `project context create`); `build` never makes one implicitly. Under the
//! in-place strategy, building a non-current branch switches the focus's own-git
//! repo to it behind a [`RestoreGuard`], so the working tree is always returned
//! to where it started — even on failure.

use std::path::PathBuf;

use anyhow::{bail, Context, Result};
use clap::Args;

use wits_util::process::Command;

use crate::cmd::project::ProfileArgs;
use wits_util::build_system::{backend_for, Backend, BuildMode, EmitContext};
use wits_util::git::{Repository, RestoreGuard};
use wits_util::project::model::{BranchStrategy, Profile};
use wits_util::project::resolve::{self, Plan, PlanInput, ToolchainInjector};
use wits_util::project::resolve_target;
use wits_util::project::workspace::{ProjectData, Workspace};

#[derive(Debug, Args)]
pub struct BuildArgs {
    /// Project name or path (default: the project owning the current directory).
    #[arg(value_name = "NAME|PATH")]
    pub target: Option<String>,
    #[command(flatten)]
    pub profile: ProfileArgs,

    /// Configure only; do not compile.
    #[arg(long = "config-only", conflicts_with_all = ["build_only", "reconfig", "uninstall"])]
    pub config_only: bool,
    /// Compile only; assume already configured.
    #[arg(long = "build-only", conflicts_with_all = ["reconfig", "uninstall"])]
    pub build_only: bool,
    /// Delete the build dir and configure fresh.
    #[arg(long, conflicts_with = "uninstall")]
    pub reconfig: bool,
    /// Reverse an install (backend-driven).
    #[arg(long)]
    pub uninstall: bool,
    /// Install after building.
    #[arg(long)]
    pub install: bool,
    /// Override the install prefix, ignoring the project's configured
    /// `install_dir` (the backend's install-prefix, e.g. cmake's
    /// `CMAKE_INSTALL_PREFIX`). Affects configure as well as install.
    #[arg(long = "install-dir", value_name = "DIR")]
    pub install_dir: Option<PathBuf>,
    /// Build a specific target.
    #[arg(short = 't', long = "target")]
    pub build_target: Option<String>,

    /// Raw args appended to the configure command (verbatim).
    #[arg(long = "extra-config-args", num_args = 1.., value_name = "ARG")]
    pub extra_config_args: Vec<String>,
    /// Raw args appended to the build command (verbatim).
    #[arg(long = "extra-build-args", num_args = 1.., value_name = "ARG")]
    pub extra_build_args: Vec<String>,
    /// Raw args appended to the install command (verbatim).
    #[arg(long = "extra-install-args", num_args = 1.., value_name = "ARG")]
    pub extra_install_args: Vec<String>,
    /// Shorthand: -Xconfig,ARG / -Xbuild,ARG / -Xinstall,ARG (repeatable).
    #[arg(short = 'X', value_name = "SCOPE,ARG")]
    pub extra: Vec<String>,
}

/// What a build *does*, not where it resolves to (that is `project::Profile`).
/// Extra args are verbatim and applied last, at the highest priority (§5.5).
/// Lives here, not in `project::model`, because nothing outside this module
/// reads it — `resolve::plan` only ever needs the three extra-args lists,
/// passed separately so `project` doesn't need to know this type exists.
#[derive(Debug, Clone, Default)]
pub struct BuildOptions {
    pub mode: BuildMode,
    pub install: bool,
    /// A command-line override of the resolved install prefix (§5.5); `None`
    /// leaves the project's configured `install_dir` in force.
    pub install_dir: Option<PathBuf>,
    pub target: Option<String>,
    pub extra_config_args: Vec<String>,
    pub extra_build_args: Vec<String>,
    pub extra_install_args: Vec<String>,
}

/// `wits build` — its own top-level command, over the shared `project` core.
pub fn run(args: &BuildArgs) -> Result<()> {
    let ws = Workspace::load()?;
    let project = resolve_target(&ws, args.target.as_deref())?;
    execute(
        &ws,
        project,
        &args.profile.to_profile(),
        &build_options(args)?,
    )
}

fn build_options(a: &BuildArgs) -> Result<BuildOptions> {
    let mode = if a.config_only {
        BuildMode::ConfigOnly
    } else if a.build_only {
        BuildMode::BuildOnly
    } else if a.reconfig {
        BuildMode::Reconfig
    } else if a.uninstall {
        BuildMode::Uninstall
    } else {
        BuildMode::Auto
    };

    let (mut cfg, mut build, mut install) = (
        a.extra_config_args.clone(),
        a.extra_build_args.clone(),
        a.extra_install_args.clone(),
    );
    for x in &a.extra {
        let (scope, arg) = x
            .split_once(',')
            .with_context(|| format!("-X expects SCOPE,ARG (got '{x}')"))?;
        match scope {
            "config" => cfg.push(arg.to_owned()),
            "build" => build.push(arg.to_owned()),
            "install" => install.push(arg.to_owned()),
            other => bail!("-X scope must be config|build|install (got '{other}')"),
        }
    }

    Ok(BuildOptions {
        mode,
        install: a.install,
        install_dir: a.install_dir.clone(),
        target: a.build_target.clone(),
        extra_config_args: cfg,
        extra_build_args: build,
        extra_install_args: install,
    })
}

fn execute(
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

    // Resolve the backend once, from the project's declared build_system — it is
    // both the L0 toolchain injector for planning and the step emitter below.
    // (`build_system` is not profile-overridable, so this matches `plan`.) The
    // enum is total, so there is no "unsupported" error path here; an unknown
    // name was rejected when the project file was parsed.
    let backend = project.project.build_system.map(backend_for);

    let mut plan = make_plan(ws, project, profile, opts, &branch, backend.as_deref())?;

    // A `--install-dir` on the command line overrides the project's resolved
    // prefix (§5.5, highest priority). `install_dir` only feeds the backend's
    // install-prefix step, so overriding the final plan value is sufficient.
    if let Some(dir) = &opts.install_dir {
        plan.install_dir = Some(dir.clone());
    }

    let Some(build_dir) = plan.build_dir.clone() else {
        log::warn!(
            "project '{}': no build_dir configured — nothing to build",
            project.name
        );
        return Ok(());
    };
    let be = backend.context("build_dir is set but build_system is not")?;
    log::debug!("backend: {}", be.name());

    // Own-git repo the in-place dance acts on; kept alive for the build scope so
    // the restore guard can borrow it.
    let identity_git = match (plan.strategy, &plan.identity_repo) {
        (BranchStrategy::InPlace, Some(name)) => {
            Some(Repository::new(project.repo_abs_path(name).with_context(
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
        source_dir: &plan.source_dir,
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
    be: Option<&dyn Backend>,
) -> Result<Plan> {
    // Select-vs-inject (§5.3): in auto/build-only, an already-configured build
    // dir with no explicit toolchain request is *trusted* — we skip toolchain
    // injection so a rerun does not reconfigure. Injection only shapes the L0
    // env/definitions, never the paths, so the build dir is the same either way.
    //
    // Whether we're even *eligible* to trust is known without planning (mode +
    // explicit-toolchain); only "is it already configured?" needs the build dir.
    // So when eligible we plan the no-injection form first — the exact plan we
    // keep if we do trust — and re-plan with injection only when the dir turns
    // out to be unconfigured (a one-time first configure). Every subsequent
    // rerun of a configured tree, the frequent path, plans just once.
    let explicit_toolchain =
        profile.toolchain.is_some() || std::env::var_os("WITS_PROJECT_TOOLCHAIN").is_some();
    let trust_eligible =
        matches!(opts.mode, BuildMode::Auto | BuildMode::BuildOnly) && !explicit_toolchain;

    if !trust_eligible {
        return plan_with(ws, project, profile, opts, branch, true, be);
    }

    let plan = plan_with(ws, project, profile, opts, branch, false, be)?;
    let configured = plan
        .build_dir
        .as_ref()
        .zip(be)
        .is_some_and(|(bd, be)| be.is_configured(bd));
    if configured {
        Ok(plan)
    } else {
        plan_with(ws, project, profile, opts, branch, true, be)
    }
}

fn plan_with(
    ws: &Workspace,
    project: &ProjectData,
    profile: &Profile,
    opts: &BuildOptions,
    branch: &str,
    inject_toolchain: bool,
    be: Option<&dyn Backend>,
) -> Result<Plan> {
    resolve::plan(
        ws,
        project,
        &PlanInput {
            profile,
            branch,
            inject_toolchain,
            injector: be.map(|b| b as &dyn ToolchainInjector),
            extra_config_args: &opts.extra_config_args,
            extra_build_args: &opts.extra_build_args,
            extra_install_args: &opts.extra_install_args,
        },
    )
}

/// Set up the in-place dance for the identity repo, returning a guard that
/// restores it. `None` when no switch is needed (already on the target).
fn prepare_in_place<'a>(git: &'a Repository, plan: &Plan) -> Result<Option<RestoreGuard<'a>>> {
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
    if git.stash_push("wits project auto-stash")? {
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
    Repository::new(path)
        .current_branch()
        .context("detached HEAD is unsupported; pass --branch")
}
