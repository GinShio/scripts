//! `wf build` — branch-aware build orchestration.
//!
//! Formerly the standalone `builder` Python script.
//!
//! # Planned features
//!
//! - **Branch-to-preset mapping**: automatically select a build preset based
//!   on the current Git branch name.
//! - **Toolchain management**: discover and validate toolchains defined in
//!   the TOML configuration.
//! - **Environment composition**: merge base environments with
//!   branch-/preset-specific overlays before invoking the build system.
//! - **Artifact validation**: verify that expected output paths exist after
//!   the build completes.
//!
//! # Configuration schema (planned)
//!
//! ```toml
//! [build]
//! system = "cmake"         # "cmake" | "meson" | "make" | "cargo"
//! source_dir = "."
//! build_dir  = "_build"
//!
//! [[build.presets]]
//! name    = "release"
//! branch  = "main"
//! options = ["-DCMAKE_BUILD_TYPE=Release"]
//! ```

use clap::Args;

use crate::cli::GlobalOptions;

/// Arguments for `wf build`.
#[derive(Debug, Args)]
pub struct BuildArgs {
    /// Force a specific build preset, ignoring branch-based auto-selection.
    #[arg(short, long, value_name = "PRESET")]
    pub preset: Option<String>,

    /// Pass additional flags directly to the underlying build system.
    #[arg(last = true, value_name = "EXTRA_ARGS")]
    pub extra: Vec<String>,
}

/// Entry point for `wf build`.
pub fn run(_global: &GlobalOptions, _args: &BuildArgs) -> anyhow::Result<()> {
    // TODO: implement branch-aware build orchestration
    log::info!("wf build — not yet implemented");
    Ok(())
}
