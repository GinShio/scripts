//! Larger, self-contained subsystems that commands compose.
//!
//! Where `core` is the thin floor — how we talk to the OS, git, and config —
//! `util` is for building blocks with real logic of their own. The line matters
//! because it keeps `core` from accreting domain knowledge: anything that
//! "knows" about git hosting platforms or remote URL shapes, what a project
//! *is*, or how a build system spells its flags belongs here, behind its own
//! seam, so a command depends on a small interface rather than on the tangle
//! underneath.

pub mod build_system;
pub mod forge;
pub mod project;
pub mod remote;
