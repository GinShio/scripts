//! `wf project` — build, update, and introspect source projects from one
//! declarative registry.
//!
//! The shape mirrors the design: a small read-only **core** (`model`, `workspace`,
//! `resolve`) that describes and resolves without side effects, and heavier
//! **actions** (`build`, `update`, `context`) layered on top. The CLI here is a
//! thin shell over the two. See `docs/project/design.md` for the reasoning.

pub mod backend;
pub mod build;
pub mod context;
pub mod git;
pub mod model;
pub mod resolve;
pub mod update;
pub mod workspace;

use anyhow::{bail, Context, Result};
use clap::{Args, Subcommand};

use model::{BuildMode, BuildOptions, Profile};
use workspace::{expand_tilde, looks_like_path, ProjectData, Workspace};

#[derive(Debug, Args)]
pub struct ProjectArgs {
    #[command(subcommand)]
    pub action: ProjectAction,
}

#[derive(Debug, Subcommand)]
pub enum ProjectAction {
    /// Describe projects, or validate their configuration with --check.
    Info(InfoArgs),
    /// Configure and build a project (and optionally install / uninstall).
    Build(BuildArgs),
    /// Refresh git for every repo of a project.
    Update(UpdateArgs),
    /// Manage a branch's build context (worktree + build dir).
    Context(ContextArgs),
}

/// The profile axes shared by `info` and `build` (they affect resolution).
#[derive(Debug, Args, Default)]
pub struct ProfileArgs {
    /// Target branch (the build identity). Default: the focus repo's current branch.
    #[arg(short = 'b', long)]
    pub branch: Option<String>,
    /// Build type — lowercase, meson-aligned (debug, release, …).
    #[arg(short = 'B', long = "build-type")]
    pub build_type: Option<String>,
    /// Select a declared toolchain.
    #[arg(short = 'T', long)]
    pub toolchain: Option<String>,
    /// Build-system generator (e.g. Ninja).
    #[arg(short = 'G', long)]
    pub generator: Option<String>,
    /// Apply a preset (repeatable; accepts org/preset).
    #[arg(short = 'p', long = "preset")]
    pub presets: Vec<String>,
    /// Override which repo is the focus.
    #[arg(long)]
    pub focus: Option<String>,
}

impl ProfileArgs {
    fn to_profile(&self) -> Profile {
        Profile {
            build_type: self.build_type.clone(),
            toolchain: self.toolchain.clone(),
            generator: self.generator.clone(),
            branch: self.branch.clone(),
            presets: self.presets.clone(),
            focus: self.focus.clone(),
        }
    }

    fn any_set(&self) -> bool {
        self.branch.is_some()
            || self.build_type.is_some()
            || self.toolchain.is_some()
            || self.generator.is_some()
            || !self.presets.is_empty()
    }
}

#[derive(Debug, Args)]
pub struct InfoArgs {
    /// Project name, or a path inside one (default: list every project).
    #[arg(value_name = "NAME|PATH")]
    pub target: Option<String>,
    /// Validate configuration legality instead of describing.
    #[arg(long)]
    pub check: bool,
    #[command(flatten)]
    pub profile: ProfileArgs,
}

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

#[derive(Debug, Args)]
pub struct UpdateArgs {
    /// Project name or path (default: the project owning the current directory).
    #[arg(value_name = "NAME|PATH")]
    pub target: Option<String>,
}

#[derive(Debug, Args)]
pub struct ContextArgs {
    #[command(subcommand)]
    pub action: ContextAction,
}

#[derive(Debug, Subcommand)]
pub enum ContextAction {
    /// Materialise a branch's build context (worktree; no-op in-place).
    Create(ContextItemArgs),
    /// Tear down a branch's build context (worktree + build dir).
    Prune(ContextItemArgs),
}

#[derive(Debug, Args)]
pub struct ContextItemArgs {
    /// Project name or path (default: the project owning the current directory).
    #[arg(value_name = "NAME|PATH")]
    pub target: Option<String>,
    /// The branch whose context to create/prune.
    #[arg(short = 'b', long)]
    pub branch: String,
    /// Override which repo is the focus.
    #[arg(long)]
    pub focus: Option<String>,
    /// Remove even a dirty worktree (prune only).
    #[arg(long)]
    pub force: bool,
}

pub fn run(args: &ProjectArgs) -> Result<()> {
    let ws = Workspace::load()?;
    match &args.action {
        ProjectAction::Info(a) => info(&ws, a),
        ProjectAction::Build(a) => {
            let project = resolve_target(&ws, a.target.as_deref())?;
            build::run(&ws, project, &a.profile.to_profile(), &build_options(a)?)
        }
        ProjectAction::Update(a) => {
            let project = resolve_target(&ws, a.target.as_deref())?;
            update::run(&ws, project)
        }
        ProjectAction::Context(a) => run_context(&ws, a),
    }
}

fn run_context(ws: &Workspace, args: &ContextArgs) -> Result<()> {
    let item = match &args.action {
        ContextAction::Create(i) | ContextAction::Prune(i) => i,
    };
    let project = resolve_target(ws, item.target.as_deref())?;
    let profile = Profile {
        focus: item.focus.clone(),
        branch: Some(item.branch.clone()),
        ..Default::default()
    };
    match &args.action {
        ContextAction::Create(_) => context::create(ws, project, &profile, &item.branch),
        ContextAction::Prune(_) => context::prune(ws, project, &profile, &item.branch, item.force),
    }
}

/// Resolve a name/path positional (or the current directory) to one project.
fn resolve_target<'a>(ws: &'a Workspace, target: Option<&str>) -> Result<&'a ProjectData> {
    match target {
        Some(t) if looks_like_path(t) => {
            let path = expand_tilde(t);
            ws.project_for_path(&path)
                .with_context(|| format!("no project owns the path {}", path.display()))
        }
        Some(t) => ws.project(t),
        None => {
            let cwd = std::env::current_dir()?;
            ws.project_for_path(&cwd).context(
                "not inside any known project; pass a name or run from inside a project's checkout",
            )
        }
    }
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
        target: a.build_target.clone(),
        extra_config_args: cfg,
        extra_build_args: build,
        extra_install_args: install,
    })
}

// --- info ---------------------------------------------------------------------

fn info(ws: &Workspace, args: &InfoArgs) -> Result<()> {
    if args.check {
        return check(ws, args.target.as_deref());
    }
    match &args.target {
        None => {
            for project in ws.projects() {
                println!("{}", summary_line(project));
            }
            Ok(())
        }
        Some(_) => {
            let project = resolve_target(ws, args.target.as_deref())?;
            describe(ws, project, &args.profile)
        }
    }
}

fn summary_line(project: &ProjectData) -> String {
    let bs = project.project.build_system.as_deref().unwrap_or("-");
    let focus = project.focus_name(None);
    format!("{:<24} focus={:<8} build={}", project.key(), focus, bs)
}

fn describe(ws: &Workspace, project: &ProjectData, profile: &ProfileArgs) -> Result<()> {
    println!("project: {}", project.key());
    println!("  source: {}", project.source.display());
    if let Some(org) = &project.org {
        println!("  org:   {org}");
    }
    println!("  focus: {}", project.focus_name(profile.focus.as_deref()));
    if let Some(bs) = &project.project.build_system {
        println!("  build: {bs}");
    }
    if let Some(tc) = &project.project.toolchain {
        println!("  toolchain: {tc}");
    }

    println!("  repos:");
    for name in project.repos.keys() {
        let kind = project.kind_of(name).map(|k| k.as_str()).unwrap_or("?");
        let path = project
            .repo_abs_path(name)
            .map(|p| p.display().to_string())
            .unwrap_or_default();
        let git = git::Git::new(&path);
        let state = if git.is_repo() {
            let branch = git.current_branch().unwrap_or_else(|| "-".into());
            let commit = git.head_commit().unwrap_or_else(|| "-".into());
            format!("{branch} @ {commit}")
        } else {
            "<not cloned>".into()
        };
        println!("    {name:<10} {kind:<10} {state:<24} {path}");
        for wt in git.worktrees() {
            if wt.path != std::path::Path::new(&path) {
                let b = wt.branch.as_deref().unwrap_or("-");
                println!("      worktree {b:<16} {}", wt.path.display());
            }
        }
    }

    // Resolved paths when a profile is supplied (or a current branch is known);
    // otherwise show the raw templates, since resolution needs a branch.
    let branch = profile.branch.clone().or_else(|| {
        resolve::identity_repo(project, project.focus_name(profile.focus.as_deref()))
            .and_then(|n| project.repo_abs_path(&n))
            .and_then(|p| git::Git::new(p).current_branch())
    });
    match branch {
        Some(branch) if profile.any_set() || true => {
            let opts = BuildOptions::default();
            let plan = resolve::plan(
                ws,
                project,
                &resolve::PlanInput {
                    profile: &profile.to_profile(),
                    options: &opts,
                    branch: &branch,
                    inject_toolchain: false,
                },
            )?;
            println!(
                "  resolved (branch {}, {}):",
                plan.branch_slug, plan.build_type
            );
            println!("    focus:       {}", plan.focus);
            if let Some(tc) = &plan.toolchain {
                println!("    toolchain:   {}", tc.name);
            }
            println!("    work.dir:    {}", plan.work_dir.display());
            if let Some(b) = &plan.build_dir {
                println!("    build_dir:   {}", b.display());
            }
            if let Some(i) = &plan.install_dir {
                println!("    install_dir: {}", i.display());
            }
        }
        _ => {
            if let Some(t) = &project.project.build_dir {
                println!("  build_dir (template):   {t}");
            }
            if let Some(t) = &project.project.install_dir {
                println!("  install_dir (template): {t}");
            }
        }
    }
    Ok(())
}

// --- info --check -------------------------------------------------------------

fn check(ws: &Workspace, target: Option<&str>) -> Result<()> {
    let projects: Vec<&ProjectData> = match target {
        Some(_) => vec![resolve_target(ws, target)?],
        None => ws.projects().collect(),
    };
    let mut problems = Vec::new();
    for project in projects {
        for issue in check_one(ws, project) {
            problems.push(format!("[{}] {issue}", project.key()));
        }
    }
    if problems.is_empty() {
        println!("ok");
        Ok(())
    } else {
        for p in &problems {
            eprintln!("{p}");
        }
        bail!("{} configuration problem(s)", problems.len())
    }
}

fn check_one(ws: &Workspace, project: &ProjectData) -> Vec<String> {
    let mut issues = Vec::new();

    for (name, repo) in &project.repos {
        if project.kind_of(name).is_some_and(|k| k.has_own_git()) && repo.main_branch.is_none() {
            issues.push(format!("repo '{name}' has its own git but no main_branch"));
        }
    }

    let p = &project.project;
    if p.build_dir.is_some() && p.build_system.is_none() {
        issues.push("build_dir is set but build_system is not".into());
    }
    if let Some(bs) = &p.build_system {
        if backend::for_system(bs).is_none() {
            issues.push(format!("unsupported build_system '{bs}'"));
        }
        if let Some(tc) = &p.toolchain {
            if let Some(def) = ws.toolchains().get(tc) {
                if !def.supports.is_empty() && !def.supports.iter().any(|s| s == bs) {
                    issues.push(format!("toolchain '{tc}' does not support '{bs}'"));
                }
            }
        }
    }
    if let Some(tc) = &p.toolchain {
        if !ws.toolchains().contains_key(tc) {
            issues.push(format!("unknown toolchain '{tc}'"));
        }
    }

    // A dry resolve catches template errors, preset cycles, unknown presets.
    let profile = Profile {
        toolchain: p.toolchain.clone(),
        ..Default::default()
    };
    let opts = BuildOptions::default();
    if let Err(e) = resolve::plan(
        ws,
        project,
        &resolve::PlanInput {
            profile: &profile,
            options: &opts,
            branch: "main",
            inject_toolchain: true,
        },
    ) {
        issues.push(format!("resolution: {e:#}"));
    }
    issues
}
