//! The read-only project core: describe and resolve projects without side
//! effects.
//!
//! This is the one place that knows what a project *is* — `model`, `workspace`,
//! and `resolve` describe and resolve without side effects, and `git` is the
//! project-shaped git surface those actions drive. It lives under `util` (not
//! `cmd`) because it is a self-contained subsystem the commands *compose*, not a
//! command itself: the `wf project` CLI shell (`cmd::project`), and the separate
//! `wf build` / `wf update` commands (`cmd::build`, `cmd::update`), are all
//! consumers of this public API ([`resolve_target`], `resolve::plan`, `git`),
//! not peers sharing its internals.
//!
//! The build systems are *not* here: they are a build-time concern, so the core
//! never names a backend. Its only tie to them is the
//! `resolve::ToolchainInjector` seam, which the core owns and each backend in
//! [`crate::util::build_system`] implements. See `docs/project/design.md` §1.4.

pub mod git;
pub mod model;
pub mod resolve;
pub mod workspace;

use anyhow::{Context, Result};

use workspace::{expand_tilde, looks_like_path, ProjectData, Workspace};

/// Resolve a name/path positional (or the current directory) to one project.
///
/// The core's public entry point for turning a `--target`-shaped positional
/// into a project: `wf project` (`info`/`context`), `wf build`, and `wf update`
/// all funnel through here, so the name-vs-path rules stay in one place
/// (§1.4 of `docs/project/design.md`).
pub fn resolve_target<'a>(ws: &'a Workspace, target: Option<&str>) -> Result<&'a ProjectData> {
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
