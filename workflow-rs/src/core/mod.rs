//! Building blocks shared between subcommands.
//!
//! These modules exist because the same handful of concerns — running a
//! subprocess, reading a setting, talking to git, encrypting bytes — keep
//! showing up across workflow tooling, and each one has a sharp edge that is
//! easy to get subtly wrong. Centralising them means the edge only has to be
//! reasoned about once.
//!
//! Only what the current commands actually use lives here. When a new command
//! needs a capability we don't have yet (streaming subprocess output, a richer
//! git surface, file-based config), it gets added alongside that command so it
//! can be designed against a real caller rather than a guess.

pub mod crypto;
pub mod git;
pub mod log;
pub mod process;
pub mod resolver;
