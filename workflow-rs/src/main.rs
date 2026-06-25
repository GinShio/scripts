//! `wf` — Unified Workflow CLI
//!
//! Single binary wrapping multiple workflow tools previously implemented as
//! independent Python scripts.  Written in Rust with Meson as the meta build
//! system and Cargo for dependency management.
//!
//! # Global flags
//!
//! | Flag | Effect |
//! |---|---|
//! | `-v, --verbose` | Enable `DEBUG`-level log output |
//! | `-n, --dry-run` | Print what would be executed without running it |
//! | `-c, --config <PATH>` | Explicit TOML v1.0 configuration file |
//!
//! # Subcommands
//!
//! | Command | Description |
//! |---|---|
//! | `wf build` | Branch-aware build orchestration |
//! | `wf stack` | Stacked PR management and remote sync |
//! | `wf gpu` | GPU test automation |
//! | `wf remote` | Git remotes setup and mirroring |
//! | `wf crypt` | Transparent file encryption for Git |

use clap::{Parser, Subcommand};

mod cli;
mod cmd;
mod core;

use cli::GlobalOptions;

// ---------------------------------------------------------------------------
// CLI definition (clap derive)
// ---------------------------------------------------------------------------

#[derive(Debug, Parser)]
#[command(
    name = "wf",
    version,
    about = "Unified Workflow CLI",
    long_about = "High-performance, unified rewrite of the Python-based workflow script collection.",
    // Show help when invoked without a subcommand.
    arg_required_else_help = true,
)]
struct Cli {
    /// Enable verbose / debug logging.
    #[arg(short = 'v', long, global = true)]
    verbose: bool,

    /// Show what would be done without executing any commands.
    #[arg(short = 'n', long, global = true)]
    dry_run: bool,

    /// Path to the TOML v1.0 configuration file.
    #[arg(short = 'c', long, value_name = "PATH", global = true)]
    config: Option<String>,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Debug, Subcommand)]
enum Commands {
    /// Branch-aware build orchestration.
    Build(cmd::builder::BuildArgs),
    /// Stacked PR management and remote synchronisation.
    Stack(cmd::stack::StackArgs),
    /// GPU test automation and environment management.
    Gpu(cmd::gpu::GpuArgs),
    /// Git remotes setup and mirroring.
    Remote(cmd::remote::RemoteArgs),
    /// Transparent file encryption for Git.
    Crypt(cmd::crypt::CryptArgs),
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();

    let global = GlobalOptions {
        verbose: cli.verbose,
        dry_run: cli.dry_run,
        config_path: cli.config,
    };

    // Initialise the global logging and dry-run state.
    core::log::init(global.verbose, global.dry_run);

    match &cli.command {
        Commands::Build(args) => cmd::builder::run(&global, args),
        Commands::Stack(args) => cmd::stack::run(&global, args),
        Commands::Gpu(args) => cmd::gpu::run(&global, args),
        Commands::Remote(args) => cmd::remote::run(&global, args),
        Commands::Crypt(args) => cmd::crypt::run(&global, args),
    }
}
