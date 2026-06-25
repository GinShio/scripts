//! Core library modules shared across all `wf` subcommands.
//!
//! | Module | Purpose |
//! |---|---|
//! | [`log`] | Global verbose/dry-run flags and custom logger |
//! | [`process`] | Fluent command builder with dry-run support |
//! | [`git`] | Pure CLI-based Git repository API |
//! | [`config`] | TOML v1.0 configuration loading and deep merging |
//! | [`crypto`] | AEAD encryption/decryption with SIV deterministic mode |

pub mod config;
pub mod crypto;
pub mod git;
pub mod log;
pub mod process;
