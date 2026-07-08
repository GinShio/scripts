//! `wits-util` — the shared library behind the `wits` CLI and its plugins.
//!
//! Everything a command needs that isn't the command itself lives here: the thin
//! OS/git/process floor, the template and config machinery, and the larger
//! self-contained subsystems (project resolution, build systems, forge/remote).
//! The `wits` binary composes these into built-in subcommands; an out-of-tree
//! plugin binary (`wits-<name>`) can depend on this same crate to reuse the
//! floor instead of reinventing it.
//!
//! The modules are flat on purpose. There is still a rough gradient — `config`,
//! `crypto`, `git`, `log`, `process`, `resolver`, `template` are the thin floor;
//! `build_system`, `forge`, `project`, `remote` are subsystems with real domain
//! logic — but they sit side by side so a consumer names `wits_util::process` or
//! `wits_util::forge` directly, without a grouping layer in between.

pub mod build_system;
pub mod config;
pub mod crypto;
pub mod forge;
pub mod git;
pub mod log;
pub mod process;
pub mod project;
pub mod remote;
pub mod resolver;
pub mod template;
