//! Subcommand implementations, one module per `wf <command>`.
//!
//! Right now `transcrypt` is the only command that exists. The umbrella binary
//! is kept deliberately thin so that a new command is just a new module here
//! plus one arm in `main`'s dispatch — there is no plugin machinery to learn,
//! and nothing speculative is carried around for commands that don't exist yet.

pub mod stack;
pub mod transcrypt;
