//! `wits project` — the CLI shell over the read-only project core.
//!
//! Describes projects (the default) or validates their configuration
//! (`--check`), and nests one action, `context` (`wits project context`), since
//! that action *is* CLI-nested. Everything about what a project *is* — the
//! model, the workspace registry, resolution, and the project-shaped git
//! surface — lives in the read-only core at [`wits_util::project`]; this
//! module is one of its consumers, alongside the separate `wits build` and
//! `wits update` commands. See `docs/project/design.md` §1.4.

pub mod context;

use anyhow::{bail, Result};
use clap::{Args, Subcommand};

use anyhow::Context;

use wits_util::git;
use wits_util::project::model::Profile;
use wits_util::project::workspace::{expand_tilde, looks_like_path, ProjectData, Workspace};
use wits_util::project::{resolve, resolve_target};

/// `wits project` — describe projects (the default), or manage a build context.
#[derive(Debug, Args)]
#[command(args_conflicts_with_subcommands = true)]
pub struct ProjectArgs {
    #[command(subcommand)]
    pub command: Option<ProjectSub>,
    #[command(flatten)]
    pub info: InfoArgs,
}

#[derive(Debug, Subcommand)]
pub enum ProjectSub {
    /// Manage a branch's build context (worktree + build dir).
    Context(ContextArgs),
    /// Print the main branch of the repo you are in (or a named project) —
    /// the machine-readable answer scripts and git hooks need.
    MainBranch(RepoQueryArgs),
    /// Print the resolved build directory for a branch, one line, for scripts.
    BuildDir(PathQueryArgs),
    /// Print the resolved install prefix for a branch, one line, for scripts.
    InstallDir(PathQueryArgs),
    /// Print the resolved source directory (where the build configures from).
    SourceDir(PathQueryArgs),
    /// Print the branch's checkout root (`work.dir`) — the path templates anchor on.
    WorkDir(PathQueryArgs),
}

/// A repo-scoped query anchored by name or path (default: the current dir).
#[derive(Debug, Args)]
pub struct RepoQueryArgs {
    /// Project name, or a path inside a checkout (default: the current dir).
    #[arg(value_name = "NAME|PATH")]
    pub target: Option<String>,
}

/// A plan-path query: like [`RepoQueryArgs`], plus the branch whose resolved
/// paths to report. Shared by `build-dir` / `install-dir` / `source-dir` /
/// `work-dir`, which differ only in which resolved path they print.
#[derive(Debug, Args)]
pub struct PathQueryArgs {
    /// Project name, or a path inside a checkout (default: the current dir).
    #[arg(value_name = "NAME|PATH")]
    pub target: Option<String>,
    /// The branch to resolve for (default: the anchored repo's current branch).
    #[arg(short = 'b', long)]
    pub branch: Option<String>,
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
    pub fn to_profile(&self) -> Profile {
        Profile {
            build_type: self.build_type.clone(),
            toolchain: self.toolchain.clone(),
            generator: self.generator.clone(),
            branch: self.branch.clone(),
            presets: self.presets.clone(),
            focus: self.focus.clone(),
        }
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

/// `wits project` (and `wits project context`).
pub fn run(args: &ProjectArgs) -> Result<()> {
    let ws = Workspace::load()?;
    match &args.command {
        Some(ProjectSub::Context(c)) => run_context(&ws, c),
        Some(ProjectSub::MainBranch(a)) => main_branch(&ws, a),
        Some(ProjectSub::BuildDir(a)) => build_dir(&ws, a),
        Some(ProjectSub::InstallDir(a)) => install_dir(&ws, a),
        Some(ProjectSub::SourceDir(a)) => source_dir(&ws, a),
        Some(ProjectSub::WorkDir(a)) => work_dir(&ws, a),
        None => info(&ws, &args.info),
    }
}

// --- machine-readable queries (for scripts / git hooks) -----------------------

/// Resolve a target to `(project, anchor-repo)`: a path (or the current dir)
/// resolves to the *containing* repo, a name to the project's focus repo.
fn resolve_repo<'a>(ws: &'a Workspace, target: Option<&str>) -> Result<(&'a ProjectData, String)> {
    match target {
        None => {
            let cwd = std::env::current_dir()?;
            ws.repo_for_path(&cwd).context(
                "not inside any known project; pass a name or run from inside a project's checkout",
            )
        }
        Some(t) if looks_like_path(t) => {
            let path = expand_tilde(t);
            ws.repo_for_path(&path)
                .with_context(|| format!("no project owns the path {}", path.display()))
        }
        Some(name) => {
            let project = ws.project(name)?;
            Ok((project, project.focus_name(None).to_owned()))
        }
    }
}

/// The main branch that governs the anchored repo: its identity repo's
/// `main_branch` (a subtree inherits its anchor's). One line to stdout.
fn main_branch(ws: &Workspace, args: &RepoQueryArgs) -> Result<()> {
    let (project, repo) = resolve_repo(ws, args.target.as_deref())?;
    let identity = resolve::identity_repo(project, &repo).unwrap_or(repo);
    let mb = project
        .repos
        .get(&identity)
        .and_then(|r| r.main_branch.clone())
        .with_context(|| {
            format!(
                "repo '{identity}' of project '{}' has no main_branch",
                project.key()
            )
        })?;
    println!("{mb}");
    Ok(())
}

/// Resolve a branch's build [`Plan`](resolve::Plan) for a path query, anchored
/// like [`resolve_repo`] with the branch defaulting to the anchored repo's
/// current one. Shared by the `*-dir` queries below, which differ only in the
/// resolved path they print.
fn resolve_plan<'a>(
    ws: &'a Workspace,
    args: &PathQueryArgs,
) -> Result<(&'a ProjectData, resolve::Plan)> {
    let (project, repo) = resolve_repo(ws, args.target.as_deref())?;
    let branch = args
        .branch
        .clone()
        .or_else(|| {
            resolve::identity_repo(project, &repo)
                .and_then(|n| project.repo_abs_path(&n).ok())
                .and_then(|p| git::Repository::new(&p).current_branch())
        })
        .context("could not determine a branch; pass --branch")?;
    let profile = Profile {
        focus: Some(repo),
        branch: Some(branch),
        ..Default::default()
    };
    let plan = resolve::plan(
        ws,
        project,
        &resolve::PlanInput::paths_only(&profile, profile.branch.as_deref().unwrap_or_default()),
    )?;
    Ok((project, plan))
}

/// The resolved build directory for a branch, one line to stdout — the query a
/// checkout hook needs to point `compile_commands.json` at the active build.
fn build_dir(ws: &Workspace, args: &PathQueryArgs) -> Result<()> {
    let (project, plan) = resolve_plan(ws, args)?;
    match plan.build_dir {
        Some(dir) => {
            println!("{}", dir.display());
            Ok(())
        }
        None => bail!(
            "project '{}' has no build_dir template to resolve",
            project.key()
        ),
    }
}

/// The resolved install prefix for a branch, one line to stdout.
fn install_dir(ws: &Workspace, args: &PathQueryArgs) -> Result<()> {
    let (project, plan) = resolve_plan(ws, args)?;
    match plan.install_dir {
        Some(dir) => {
            println!("{}", dir.display());
            Ok(())
        }
        None => bail!("project '{}' has no install_dir configured", project.key()),
    }
}

/// The resolved source directory (where the backend configures from), one line.
/// Always present — it defaults to `work.dir` when no `source_dir` is declared.
fn source_dir(ws: &Workspace, args: &PathQueryArgs) -> Result<()> {
    let (_project, plan) = resolve_plan(ws, args)?;
    println!("{}", plan.source_dir.display());
    Ok(())
}

/// The branch's checkout root (`work.dir`), one line — the checkout a caller
/// `cd`s into, and the anchor every path template resolves against.
fn work_dir(ws: &Workspace, args: &PathQueryArgs) -> Result<()> {
    let (_project, plan) = resolve_plan(ws, args)?;
    println!("{}", plan.work_dir.display());
    Ok(())
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
    let bs = project
        .project
        .build_system
        .map(|b| b.as_str())
        .unwrap_or("-");
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
    if let Some(bs) = project.project.build_system {
        println!("  build: {}", bs.as_str());
    }
    if let Some(tc) = &project.project.toolchain {
        println!("  toolchain: {tc}");
    }

    println!("  repos:");
    for name in project.repos.keys() {
        let kind = project.kind_of(name).map(|k| k.as_str()).unwrap_or("?");
        // A path template that fails to resolve is a real config error; surface
        // it inline rather than letting `unwrap_or_default()` render an empty
        // path that then masquerades as a plain "<not cloned>" repo.
        let path = match project.repo_abs_path(name) {
            Ok(path) => path,
            Err(e) => {
                println!("    {name:<10} {kind:<10} <path error: {e}>");
                continue;
            }
        };
        let git = git::Repository::new(&path);
        let state = if git.is_repo() {
            let branch = git.current_branch().unwrap_or_else(|| "-".into());
            let commit = git.head_commit().unwrap_or_else(|| "-".into());
            format!("{branch} @ {commit}")
        } else {
            "<not cloned>".into()
        };
        println!("    {name:<10} {kind:<10} {state:<24} {}", path.display());
        for wt in git.worktrees() {
            if wt.path != path {
                let b = wt.branch.as_deref().unwrap_or("-");
                println!("      worktree {b:<16} {}", wt.path.display());
            }
        }
    }

    // Resolved paths when a profile is supplied (or a current branch is known);
    // otherwise show the raw templates, since resolution needs a branch.
    let branch = profile.branch.clone().or_else(|| {
        resolve::identity_repo(project, project.focus_name(profile.focus.as_deref()))
            .and_then(|n| project.repo_abs_path(&n).ok())
            .and_then(|p| git::Repository::new(&p).current_branch())
    });
    match branch {
        Some(branch) => {
            let plan = resolve::plan(
                ws,
                project,
                &resolve::PlanInput::paths_only(&profile.to_profile(), &branch),
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
            if plan.source_dir != plan.work_dir {
                println!("    source_dir:  {}", plan.source_dir.display());
            }
            if let Some(b) = &plan.build_dir {
                println!("    build_dir:   {}", b.display());
            }
            if let Some(i) = &plan.install_dir {
                println!("    install_dir: {}", i.display());
            }
        }
        _ => {
            let build_repo =
                resolve::anchor_of(project, project.focus_name(profile.focus.as_deref()));
            if let Some(t) = project
                .repos
                .get(&build_repo)
                .and_then(|r| r.source_dir.as_ref())
            {
                println!("  source_dir (template):  {t}");
            }
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
    // Whether a declared `build_system` actually has a backend is `wits build`'s
    // concern (it errors at run time); the core neither knows nor validates the
    // set of supported build systems (§1.4). Here we only cross-check the
    // *declared* facts: a toolchain's own `supports` list against `build_system`.
    if let Some(bs) = p.build_system {
        if let Some(tc) = &p.toolchain {
            if let Some(def) = ws.toolchains().get(tc) {
                if !def.supports.is_empty() && !def.supports.iter().any(|s| s == bs.as_str()) {
                    issues.push(format!(
                        "toolchain '{tc}' does not support '{}'",
                        bs.as_str()
                    ));
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
    // Validation has no backend, so this is a path-only resolve — toolchain
    // selection runs (and can fail), but there is nothing to inject.
    let profile = Profile {
        toolchain: p.toolchain.clone(),
        ..Default::default()
    };
    if let Err(e) = resolve::plan(
        ws,
        project,
        &resolve::PlanInput::paths_only(&profile, "main"),
    ) {
        issues.push(format!("resolution: {e:#}"));
    }
    issues
}
