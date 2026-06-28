//! Talking to git by shelling out to the `git` binary.
//!
//! The obvious alternative is linking libgit2, and for a tool that mostly reads
//! config that seems heavier than warranted. The deciding factor isn't weight
//! though — it's fidelity. A user's real git behaviour is the sum of their
//! `~/.gitconfig` includes, conditional includes, credential helpers, SSH
//! config and agent forwarding. libgit2 reimplements a subset of that and
//! drifts from the CLI in exactly the corners (includes, helpers) that config
//! resolution cares about. Driving the same `git` the user's shell does means
//! we read precisely what they would, with no second implementation to keep in
//! sync. The cost is a process spawn per query, which is nothing next to the
//! work these commands actually do.
//!
//! The surface here is intentionally just config reads — that's all the
//! current commands need. It will grow when a command needs it to.

use thiserror::Error;

use crate::core::process::{Command, ProcessError};

#[derive(Debug, Error)]
pub enum GitError {
    #[error("git command failed: {0}")]
    Process(#[from] ProcessError),
}

/// A handle to a repository on disk. Holds no resources and is cheap to clone;
/// it's really just the path every `git` invocation runs against.
#[derive(Debug, Clone)]
pub struct Repository {
    path: std::path::PathBuf,
}

impl Repository {
    pub fn new(path: impl Into<std::path::PathBuf>) -> Self {
        Self { path: path.into() }
    }

    fn git(&self) -> Command {
        let mut cmd = Command::new("git");
        cmd.current_dir(&self.path);
        cmd
    }

    /// Read a config value, or `None` when the key is unset. Forced because
    /// config drives control flow and must be readable even during a dry-run.
    pub fn get_config(&self, key: &str) -> Result<Option<String>, GitError> {
        let result = self
            .git()
            .args(["config", "--get", key])
            .force_run()
            .exec()?;
        if result.is_success() && !result.stdout.is_empty() {
            Ok(Some(result.stdout_trimmed().to_owned()))
        } else {
            Ok(None)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::process::Command;

    #[test]
    fn reads_a_set_value_and_reports_missing_as_none() {
        let _guard = crate::core::log::test_flag_guard();
        let dir = tempfile::tempdir().unwrap();
        // force_run: these set up state the assertion depends on, so they must
        // run even if a parallel test has flipped the global dry-run flag on.
        Command::new("git")
            .args(["init"])
            .current_dir(dir.path())
            .force_run()
            .exec()
            .unwrap();
        Command::new("git")
            .args(["config", "transcrypt.password", "hunter2"])
            .current_dir(dir.path())
            .force_run()
            .exec()
            .unwrap();

        let repo = Repository::new(dir.path());
        assert_eq!(
            repo.get_config("transcrypt.password").unwrap(),
            Some("hunter2".to_owned())
        );
        assert_eq!(repo.get_config("transcrypt.absent").unwrap(), None);
    }
}
