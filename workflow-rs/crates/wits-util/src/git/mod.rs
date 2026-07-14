//! Talking to git by shelling out to the `git` binary — the one home for every
//! git-CLI concern, at two altitudes.
//!
//! Everything here drives the real `git` the user's shell would, rather than
//! libgit2: a user's true git behaviour is the sum of their config includes,
//! conditional includes, credential helpers, and SSH setup, and libgit2
//! reimplements only a subset that drifts in exactly the corners (includes,
//! helpers, remote resolution) we care about. The cost is a process spawn per
//! call, which is nothing next to the network round-trips these tools spend
//! their time on.
//!
//! Two handles share that floor, split by altitude rather than by location (they
//! were once two files in two module trees, which only obscured that they are
//! the same concern):
//!
//! - [`Repository`] — the thin **read/ref floor**: config, branches, commits,
//!   ranges, and the review-fetch ref plumbing. Reads opt into
//!   [`force_run`](crate::process::Command::force_run) so they still answer
//!   under a dry-run, and its few mutations capture output to report precise
//!   errors.
//! - [`Git`] — the wider **working-tree surface** the `project`/`build` actions
//!   drive: worktrees, stashes, submodules, branch switches, clone. Its
//!   mutations inherit stdio so progress streams live and in colour, and a
//!   dry-run prints them instead of running.
//!
//! The split is a deliberate read-floor vs mutation-surface distinction, not an
//! accident of history; keeping both under `wits_util::git` is what makes that
//! legible.

mod repository;
mod worktree;

pub use repository::{Commit, FileChange, GitError, Repository};
pub use worktree::{clone, Git, RestoreGuard, Worktree};
