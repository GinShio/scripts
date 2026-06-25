//! Subcommand implementations.
//!
//! Each module corresponds to one `wf <subcommand>` entry point.  All
//! subcommands receive [`GlobalOptions`][crate::cli::GlobalOptions] after the
//! top-level flags have been parsed so they can consult the verbose/dry-run
//! state.

pub mod builder;
pub mod crypt;
pub mod gpu;
pub mod remote;
pub mod stack;
