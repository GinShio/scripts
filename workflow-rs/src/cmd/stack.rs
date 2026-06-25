//! `wf stack` — stacked PR management and remote synchronisation.
//!
//! Formerly the standalone `git_stack` Python script.
//!
//! # Planned features
//!
//! - **Stack slicing**: identify the commit range belonging to the current
//!   "slice" of a stacked PR chain.
//! - **Rebase helpers**: rebase one slice onto another while preserving the
//!   full stack topology.
//! - **Machete integration**: optionally delegate branch topology tracking
//!   to `git-machete`.
//! - **Remote push/sync**: push all slices in topological order, opening or
//!   updating draft PRs as needed.
//! - **Annotation support**: read branch metadata from a sidecar annotation
//!   file (`.git/wf-stack.toml`).

use clap::Args;

use crate::cli::GlobalOptions;

/// Arguments for `wf stack`.
#[derive(Debug, Args)]
pub struct StackArgs {
    #[command(subcommand)]
    pub action: Option<StackAction>,
}

/// Subcommands for `wf stack`.
#[derive(Debug, clap::Subcommand)]
pub enum StackAction {
    /// Show the current stack topology.
    Show,
    /// Push all branches in the stack to the remote.
    Push,
    /// Rebase the current branch onto its parent in the stack.
    Rebase,
}

/// Entry point for `wf stack`.
pub fn run(_global: &GlobalOptions, _args: &StackArgs) -> anyhow::Result<()> {
    // TODO: implement stacked PR orchestration
    log::info!("wf stack — not yet implemented");
    Ok(())
}
