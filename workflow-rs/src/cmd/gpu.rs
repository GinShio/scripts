//! `wf gpu` — GPU test automation and environment management.
//!
//! Formerly the standalone `gputest` Python script.
//!
//! # Planned features
//!
//! - **Run**: execute a GPU test suite inside a container or bare-metal
//!   environment, capturing pass/fail statistics.
//! - **List**: enumerate available GPU test configurations or discovered
//!   hardware.
//! - **Restore**: restore a previously saved GPU environment snapshot.
//! - **Cleanup**: remove dangling test containers or temporary artefacts.
//! - **Toolbox integration**: manage a `toolbox` / `distrobox` container
//!   image used as the GPU test environment.

use clap::Args;

use crate::cli::GlobalOptions;

/// Arguments for `wf gpu`.
#[derive(Debug, Args)]
pub struct GpuArgs {
    #[command(subcommand)]
    pub action: Option<GpuAction>,
}

/// Subcommands for `wf gpu`.
#[derive(Debug, clap::Subcommand)]
pub enum GpuAction {
    /// Run the GPU test suite.
    Run {
        /// Name of the test configuration to run.
        #[arg(value_name = "CONFIG")]
        config: Option<String>,
    },
    /// List available GPU test configurations.
    List,
    /// Restore a saved environment snapshot.
    Restore,
    /// Remove dangling containers and temporary files.
    Cleanup,
}

/// Entry point for `wf gpu`.
pub fn run(_global: &GlobalOptions, _args: &GpuArgs) -> anyhow::Result<()> {
    // TODO: implement GPU test automation
    log::info!("wf gpu — not yet implemented");
    Ok(())
}
