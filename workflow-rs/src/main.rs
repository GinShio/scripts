//! `wf` — a single binary that collects personal workflow tools behind one
//! command tree.
//!
//! The collection grows one subcommand at a time. Keeping everything in one
//! binary (rather than a pile of scripts) buys a shared core, consistent flags,
//! and a single thing to build and put on `$PATH`. Today the only inhabitant is
//! `transcrypt`; the structure here is the whole extension story — add a module
//! under `cmd/` and a match arm below.
//!
//! There are two equivalent ways to invoke a tool, the way `mount` lets you
//! write either `mount -t xfs` or `mount.xfs`: the umbrella form `wf foo` and
//! the direct form `wf-foo` / `wf.foo` (or a bare `foo` symlink). Both run this
//! one binary — the direct form is just a symlink whose name we read from
//! `argv[0]` and splice back in as the subcommand. It costs nothing (a symlink,
//! no second process) and a new command earns its direct form for free, because
//! the applet names come straight from the subcommand list rather than a table
//! we'd have to keep in sync.

use std::ffi::OsString;

use clap::{CommandFactory, Parser, Subcommand};

mod cmd;
mod core;
mod util;

#[derive(Debug, Parser)]
#[command(
    name = "wf",
    version,
    about = "Personal workflow tools, collected behind one command.",
    arg_required_else_help = true
)]
struct Cli {
    /// Show the individual git commands as they run.
    #[arg(short = 'v', long, global = true)]
    verbose: bool,

    /// Print mutating actions instead of performing them.
    ///
    /// Read-only queries still run, so control flow stays correct: a dry-run
    /// still asks git and the forge what the world looks like in order to decide
    /// what it *would* do, then prints the pushes, MR changes, and file writes
    /// rather than carrying them out. (`transcrypt` only ever reads, so it shows
    /// no effect; `stack` is where this earns its keep.)
    #[arg(short = 'n', long, global = true)]
    dry_run: bool,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Debug, Subcommand)]
enum Commands {
    /// Transparent file encryption driven by git's clean/smudge filters.
    Transcrypt(cmd::transcrypt::TranscryptArgs),
    /// Manage a stack of branches as a set of merge requests.
    Stack(cmd::stack::StackArgs),
}

fn main() -> anyhow::Result<()> {
    let cli = parse_args();
    core::log::init(cli.verbose, cli.dry_run);

    match &cli.command {
        Commands::Transcrypt(args) => cmd::transcrypt::run(args),
        Commands::Stack(args) => cmd::stack::run(args),
    }
}

/// Parse the command line, honouring busybox-style invocation. When the program
/// was run under an applet name, the subcommand is taken from `argv[0]` and the
/// remaining arguments are parsed against it; otherwise this is the plain
/// `wf <command>` path.
fn parse_args() -> Cli {
    let mut argv = std::env::args_os();
    let prog = argv.next().unwrap_or_default();

    match applet_from_prog(&prog.to_string_lossy()) {
        Some(applet) => {
            let spliced = [OsString::from("wf"), OsString::from(applet)]
                .into_iter()
                .chain(argv);
            Cli::parse_from(spliced)
        }
        None => Cli::parse(),
    }
}

/// Resolve the invoked program name to a subcommand, or `None` for the plain
/// `wf` umbrella. A leading `wf-` or `wf.` is stripped, so `wf-foo`, `wf.foo`,
/// and a bare `foo` symlink all resolve to the same applet; an unrecognised name
/// falls through to the umbrella so it simply reports an unknown command.
fn applet_from_prog(prog: &str) -> Option<String> {
    let base = prog.rsplit(['/', '\\']).next().unwrap_or(prog);
    if base == "wf" {
        return None;
    }
    let stem = base
        .strip_prefix("wf-")
        .or_else(|| base.strip_prefix("wf."))
        .unwrap_or(base);
    Cli::command()
        .get_subcommands()
        .map(|sub| sub.get_name().to_owned())
        .find(|name| name == stem)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn umbrella_name_is_not_an_applet() {
        assert_eq!(applet_from_prog("wf"), None);
        assert_eq!(applet_from_prog("/usr/local/bin/wf"), None);
    }

    #[test]
    fn dash_dot_and_bare_forms_all_resolve() {
        for prog in ["wf-transcrypt", "wf.transcrypt", "transcrypt"] {
            assert_eq!(
                applet_from_prog(prog).as_deref(),
                Some("transcrypt"),
                "{prog}"
            );
        }
    }

    #[test]
    fn the_invoked_path_is_reduced_to_its_basename() {
        assert_eq!(
            applet_from_prog("/home/me/.local/bin/wf-transcrypt").as_deref(),
            Some("transcrypt")
        );
    }

    #[test]
    fn unknown_names_fall_through_to_the_umbrella() {
        assert_eq!(applet_from_prog("wf-bogus"), None);
        assert_eq!(applet_from_prog("bogus"), None);
    }
}
