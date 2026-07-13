//! `wits update` — refresh every repo of a project.
//!
//! The default action never switches branches or touches the working tree: on a
//! feature branch it fast-forwards the `main_branch` *ref* with a refspec, which
//! is safe for a sparse checkout (nothing is materialised that wasn't already).
//! Only when you are standing on the main branch is a real fetch + `--ff-only`
//! merge used. Remote reconciliation is additive: missing remotes and mirror
//! push-URLs are added, existing ones never touched.
//!
//! A submodule is just a nested repo, so it gets the same treatment; undeclared
//! nested submodules are refreshed to their recorded commit (never `--init`,
//! which belongs to a fresh checkout — clone or worktree creation).

use std::collections::BTreeSet;
use std::path::Path;

use anyhow::{bail, Context, Result};
use clap::Args;

use wits_util::template::Engine;

use wits_util::project::git::{self, Git, RestoreGuard};
use wits_util::project::model::{infer_kind, Kind, RawRepo};
use wits_util::project::resolve;
use wits_util::project::resolve_target;
use wits_util::project::workspace::{ProjectData, Workspace};

#[derive(Debug, Args)]
pub struct UpdateArgs {
    /// Project name or path (default: the project owning the current directory).
    #[arg(value_name = "NAME|PATH")]
    pub target: Option<String>,
}

/// `wits update` — its own top-level command, over the shared `project` core.
pub fn run(args: &UpdateArgs) -> Result<()> {
    let ws = Workspace::load()?;
    let project = resolve_target(&ws, args.target.as_deref())?;
    execute(&ws, project)
}

fn execute(ws: &Workspace, project: &ProjectData) -> Result<()> {
    for name in repo_order(project) {
        let repo = &project.repos[&name];
        if infer_kind(&name, repo) == Kind::Subtree {
            continue; // shares its anchor's git; no work of its own
        }
        let path = project
            .repo_abs_path(&name)
            .with_context(|| format!("cannot resolve path of repo '{name}'"))?;
        let git = Git::new(path);
        if !git.exists() {
            clone_repo(ws, project, &name, &git)
                .with_context(|| format!("cloning repo '{name}' of project '{}'", project.name))?;
        } else {
            update_repo(ws, project, &name, &git)
                .with_context(|| format!("updating repo '{name}' of project '{}'", project.name))?;
        }
    }
    Ok(())
}

/// `repos.main` first (nested repos are cloned through it), then the rest.
fn repo_order(project: &ProjectData) -> Vec<String> {
    let mut order = Vec::new();
    if project.repos.contains_key("main") {
        order.push("main".to_owned());
    }
    for name in project.repos.keys() {
        if name != "main" {
            order.push(name.clone());
        }
    }
    order
}

fn clone_repo(ws: &Workspace, project: &ProjectData, name: &str, git: &Git) -> Result<()> {
    let repo = &project.repos[name];
    let engine = Engine::new(resolve::context_for_repo(ws, project, name));

    // A clone-phase `clone` override owns the whole thing; otherwise the default
    // clones origin, checks out the main branch, and inits submodules. `git
    // clone` creates the destination (and any leading dirs), so nothing is
    // pre-created; a `clone` override runs in the current working directory,
    // since the repo path does not exist yet.
    if let Some(action) = repo.hooks.get("clone") {
        run_hook(&engine, None, action, "clone")?;
    } else {
        // Clone from the sync source: `upstream` if declared, else `origin`. A
        // fork declared as `origin` may not exist on the server yet, so it is
        // never the clone source when an `upstream` is given — `ensure_remotes`
        // only *adds* it (no fetch) as the push target below.
        let (clone_url, remote) = match (
            repo.remotes.upstream.as_deref(),
            repo.remotes.origin.as_deref(),
        ) {
            (Some(upstream), _) => (upstream, "upstream"),
            (None, Some(origin)) => (origin, "origin"),
            (None, None) => bail!("cannot clone: no [remotes] origin or upstream declared"),
        };
        git::clone(clone_url, remote, git.path())?;
        ensure_remotes(git, repo)?;
        if let Some(mb) = &repo.main_branch {
            git.checkout(mb)?;
        }
        git.submodule_update(&git.materialised_submodules(), true)?;
    }
    run_hook_opt(
        &engine,
        Some(git.path()),
        repo.hooks.get("post_clone"),
        "post_clone",
    )?;
    Ok(())
}

fn update_repo(ws: &Workspace, project: &ProjectData, name: &str, git: &Git) -> Result<()> {
    let repo = &project.repos[name];
    let engine = Engine::new(resolve::context_for_repo(ws, project, name));

    ensure_remotes(git, repo)?;

    // Fail-fast with guaranteed restoration: if a hook or override switches the
    // branch, the guard returns us to where we started on any exit.
    let _guard = RestoreGuard::capture(git);

    run_hook_opt(
        &engine,
        Some(git.path()),
        repo.hooks.get("pre_update"),
        "pre_update",
    )?;

    if let Some(action) = repo.hooks.get("update") {
        run_hook(&engine, Some(git.path()), action, "update")?;
    } else {
        default_update(project, name, git, repo)?;
    }

    run_hook_opt(
        &engine,
        Some(git.path()),
        repo.hooks.get("post_update"),
        "post_update",
    )?;
    Ok(())
}

fn default_update(project: &ProjectData, name: &str, git: &Git, repo: &RawRepo) -> Result<()> {
    let mb = repo
        .main_branch
        .as_deref()
        .context("own-git repo has no main_branch")?;
    // The sync source that `main` advances on: `upstream` if declared, else
    // `origin`. A fork declared as `origin` need not exist — nothing fetches it.
    let sync = if repo.remotes.upstream.is_some() {
        "upstream"
    } else {
        "origin"
    };
    let on_main = git.current_branch().as_deref() == Some(mb);

    if on_main {
        git.fetch(&[sync])?;
        git.merge_ff_only(&format!("{sync}/{mb}"))?;
    } else {
        // Ref-only fast-forward: update the local main ref without a checkout,
        // so the working tree (and a sparse cone) is left untouched.
        git.fetch(&[sync, &format!("{mb}:{mb}")])?;
    }

    // Undeclared nested submodules → recorded commit; declared ones are managed
    // as their own repos, so skip their paths here.
    let declared: BTreeSet<String> = project
        .repos
        .iter()
        .filter(|(n, r)| *n != name && infer_kind(n, r) != Kind::Standalone)
        .map(|(_, r)| r.path.clone())
        .collect();
    let subs: Vec<String> = git
        .materialised_submodules()
        .into_iter()
        .filter(|p| !declared.contains(p))
        .collect();
    git.submodule_update(&subs, false)?;
    Ok(())
}

/// Additive remote reconciliation (§3.1): add what's missing, never modify.
fn ensure_remotes(git: &Git, repo: &RawRepo) -> Result<()> {
    if let Some(origin) = &repo.remotes.origin {
        git.ensure_remote("origin", origin)?;
        // Mirrors are extra push URLs on origin; git stops defaulting push to the
        // fetch URL once any push URL exists, so origin's own URL must be one too.
        git.ensure_push_url("origin", origin)?;
        for mirror in &repo.remotes.mirrors {
            git.ensure_push_url("origin", mirror)?;
        }
    }
    if let Some(upstream) = &repo.remotes.upstream {
        git.ensure_remote("upstream", upstream)?;
    }
    Ok(())
}

fn run_hook_opt(
    engine: &Engine,
    cwd: Option<&Path>,
    hook: Option<&String>,
    phase: &str,
) -> Result<()> {
    match hook {
        Some(cmd) => run_hook(engine, cwd, cmd, phase),
        None => Ok(()),
    }
}

/// Run a templated hook via `sh -c`. `cwd` selects the working directory;
/// `None` inherits the process's, which is where a `clone` override runs since
/// the repo path does not exist yet. `post_clone` and every update-phase hook
/// run in the repo path itself. A non-zero exit fails fast.
fn run_hook(engine: &Engine, cwd: Option<&Path>, command: &str, phase: &str) -> Result<()> {
    let rendered = engine
        .resolve_str(command)
        .with_context(|| format!("resolving {phase} hook"))?;
    let script = match rendered {
        wits_util::template::Value::Str(s) => s,
        other => format!("{other:?}"),
    };
    log::info!("hook {phase}: {script}");
    let mut cmd = wits_util::process::Command::new("sh");
    cmd.args(["-c".to_string(), script]);
    if let Some(dir) = cwd {
        cmd.current_dir(dir);
    }
    let code = cmd.status()?;
    if code != 0 {
        anyhow::bail!("{phase} hook failed (exit {code})");
    }
    Ok(())
}
