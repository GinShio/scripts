//! `wf remote` — Git remotes configuration and mirroring.
//!
//! Formerly the standalone `setup_remotes` Python script.
//!
//! # Planned features
//!
//! - **Initialise**: set up the standard `origin`, `upstream`, and mirror
//!   remotes for a repository based on TOML configuration and platform
//!   defaults.
//! - **Mirror management**: add or update push URLs for a fan-out mirror
//!   strategy (one push, many remotes).
//! - **SSH alias resolution**: transparently rewrite remote URLs to use
//!   SSH config aliases (e.g. `gh:` → `git@github.com:`).
//! - **Platform detection**: recognise GitHub, GitLab, Gitea, Codeberg,
//!   Bitbucket, and Azure DevOps from existing remote URLs.
//! - **Status**: show a table of all configured remotes with fetch/push URL
//!   pairs and detected platform.

use clap::Args;

use crate::cli::GlobalOptions;

/// Arguments for `wf remote`.
#[derive(Debug, Args)]
pub struct RemoteArgs {
    #[command(subcommand)]
    pub action: Option<RemoteAction>,
}

/// Subcommands for `wf remote`.
#[derive(Debug, clap::Subcommand)]
pub enum RemoteAction {
    /// Display current remote configuration.
    Status,
    /// Set up remotes according to the TOML configuration.
    Init,
    /// Add a mirror push URL to an existing remote.
    AddMirror {
        /// Remote to add the mirror to.
        #[arg(value_name = "REMOTE")]
        remote: String,
        /// Mirror URL to add.
        #[arg(value_name = "URL")]
        url: String,
    },
}

/// Entry point for `wf remote`.
pub fn run(_global: &GlobalOptions, _args: &RemoteArgs) -> anyhow::Result<()> {
    // TODO: implement remote management
    log::info!("wf remote — not yet implemented");
    Ok(())
}
