//! Pure CLI-based Git repository API.
//!
//! # Design Philosophy
//!
//! Unlike the original Python implementation which mixed `pygit2` (libgit2
//! bindings) for reads with the `git` CLI for writes, this Rust implementation
//! drives **everything** through the `git` binary.  Rationale:
//!
//! 1. **Zero library dependencies** — no `libgit2` linkage; the binary is
//!    fully self-contained.
//! 2. **100 % config compatibility** — libgit2 regularly fails to honour
//!    complex `~/.gitconfig` includes, SSH agent forwarding, and credential
//!    helpers.  The CLI always behaves exactly as the user's shell does.
//! 3. **Negligible overhead** — for workflow orchestration tasks the round-trip
//!    cost of spawning `git` is unmeasurable compared to network I/O.
//!
//! # Usage
//!
//! ```no_run
//! use wf::core::git::Repository;
//!
//! let repo = Repository::new("/path/to/repo");
//!
//! if let Some(branch) = repo.head_branch()? {
//!     println!("On branch: {branch}");
//! }
//!
//! let commit = repo.resolve_commit("HEAD")?.unwrap();
//! println!("HEAD = {commit}");
//! # Ok::<(), wf::core::git::GitError>(())
//! ```

use thiserror::Error;

use crate::core::process::{Command, ProcessError};

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

/// Errors produced by the Git API layer.
#[derive(Debug, Error)]
pub enum GitError {
    /// Underlying process execution failed.
    #[error("git command failed: {0}")]
    Process(#[from] ProcessError),
    /// Output from git was not valid UTF-8 (should never happen in practice).
    #[error("git output is not valid UTF-8: {0}")]
    Utf8(String),
}

// ---------------------------------------------------------------------------
// Repository
// ---------------------------------------------------------------------------

/// A handle to a local Git repository identified by its on-disk path.
///
/// All methods spawn `git` sub-processes with `--work-tree` / `cwd` set to
/// `path`.  The struct itself holds no OS resources; it is cheap to clone.
#[derive(Debug, Clone)]
pub struct Repository {
    path: std::path::PathBuf,
}

impl Repository {
    /// Creates a repository handle for the directory at `path`.
    ///
    /// No validation is done at construction time.  The first `git` command
    /// that runs will fail if `path` is not inside a repository.
    pub fn new(path: impl Into<std::path::PathBuf>) -> Self {
        Self { path: path.into() }
    }

    /// Returns a pre-configured [`Command`] with cwd set to this repository.
    fn git(&self) -> Command {
        let mut cmd = Command::new("git");
        cmd.current_dir(&self.path);
        cmd
    }

    // -----------------------------------------------------------------------
    // Configuration
    // -----------------------------------------------------------------------

    /// Reads a git configuration value by key (e.g. `"user.email"`).
    ///
    /// Returns `None` if the key is not set.  The call always executes even in
    /// dry-run mode because it is a read-only query.
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

    /// Sets a git configuration value.
    pub fn set_config(&self, key: &str, value: &str) -> Result<(), GitError> {
        self.git().args(["config", key, value]).exec_check()?;
        Ok(())
    }

    /// Removes a git configuration entry.
    pub fn unset_config(&self, key: &str) -> Result<(), GitError> {
        self.git().args(["config", "--unset", key]).exec_check()?;
        Ok(())
    }

    // -----------------------------------------------------------------------
    // Inspection & status
    // -----------------------------------------------------------------------

    /// Resolves a revision specifier (e.g. `"HEAD"`, `"main"`, a tag) to its
    /// full 40-character commit hash.
    ///
    /// Returns `None` for unborn branches or invalid specifiers.
    pub fn resolve_commit(&self, spec: &str) -> Result<Option<String>, GitError> {
        let result = self
            .git()
            .args(["rev-parse", "--verify", spec])
            .force_run()
            .exec()?;

        if result.is_success() && !result.stdout.is_empty() {
            Ok(Some(result.stdout_trimmed().to_owned()))
        } else {
            Ok(None)
        }
    }

    /// Returns the short name of the currently checked-out branch.
    ///
    /// Returns `None` when in detached-HEAD state.
    pub fn head_branch(&self) -> Result<Option<String>, GitError> {
        let result = self
            .git()
            .args(["symbolic-ref", "--short", "HEAD"])
            .force_run()
            .exec()?;

        if result.is_success() && !result.stdout.is_empty() {
            Ok(Some(result.stdout_trimmed().to_owned()))
        } else {
            Ok(None)
        }
    }

    /// Returns `true` if the working tree has uncommitted changes.
    ///
    /// When `include_untracked` is `false`, untracked files are ignored
    /// (equivalent to `git status --porcelain -uno`).
    pub fn is_dirty(&self, include_untracked: bool) -> Result<bool, GitError> {
        let mut cmd = self.git();
        cmd.args(["status", "--porcelain"]).force_run();
        if !include_untracked {
            cmd.arg("-uno");
        }
        let result = cmd.exec_check()?;
        Ok(!result.stdout.is_empty())
    }

    /// Returns the name of the default branch for `remote` (e.g. `"main"`).
    ///
    /// First checks `refs/remotes/<remote>/HEAD`.  Falls back to probing
    /// whether `main` or `master` exist locally.  Returns `None` if neither
    /// strategy succeeds.
    pub fn default_branch(&self, remote: &str) -> Result<Option<String>, GitError> {
        let ref_path = format!("refs/remotes/{remote}/HEAD");
        let result = self
            .git()
            .args(["symbolic-ref", "--short", &ref_path])
            .force_run()
            .exec()?;

        if result.is_success() && !result.stdout.is_empty() {
            let trimmed = result.stdout_trimmed();
            let prefix = format!("{remote}/");
            let branch = trimmed.strip_prefix(&prefix).unwrap_or(trimmed).to_owned();
            return Ok(Some(branch));
        }

        // Fallback: check local refs
        for candidate in ["main", "master"] {
            if self.resolve_commit(candidate)?.is_some() {
                return Ok(Some(candidate.to_owned()));
            }
        }

        Ok(None)
    }

    // -----------------------------------------------------------------------
    // Remote management
    // -----------------------------------------------------------------------

    /// Returns the fetch URL of `remote`, or `None` if the remote does not exist.
    pub fn remote_url(&self, remote: &str) -> Result<Option<String>, GitError> {
        let result = self
            .git()
            .args(["remote", "get-url", remote])
            .force_run()
            .exec()?;

        if result.is_success() && !result.stdout.is_empty() {
            Ok(Some(result.stdout_trimmed().to_owned()))
        } else {
            Ok(None)
        }
    }

    /// Returns all URLs (fetch or push) configured for `remote`.
    ///
    /// Pass `push = true` to query push URLs instead of fetch URLs.
    pub fn remote_urls(&self, remote: &str, push: bool) -> Result<Vec<String>, GitError> {
        let mut cmd = self.git();
        cmd.args(["remote", "get-url", "--all"]).force_run();
        if push {
            cmd.arg("--push");
        }
        cmd.arg(remote);

        let result = cmd.exec()?;
        if !result.is_success() {
            return Ok(Vec::new());
        }

        Ok(result
            .stdout
            .lines()
            .filter(|l| !l.is_empty())
            .map(str::to_owned)
            .collect())
    }

    /// Lists all remote names configured in the repository.
    pub fn list_remotes(&self) -> Result<Vec<String>, GitError> {
        let result = self.git().arg("remote").force_run().exec()?;
        Ok(result
            .stdout
            .lines()
            .filter(|l| !l.is_empty())
            .map(str::to_owned)
            .collect())
    }

    /// Adds a new remote with the given `name` and `url`.
    pub fn add_remote(&self, name: &str, url: &str) -> Result<(), GitError> {
        self.git().args(["remote", "add", name, url]).exec_check()?;
        Ok(())
    }

    /// Renames remote `old` to `new`.
    pub fn rename_remote(&self, old: &str, new: &str) -> Result<(), GitError> {
        self.git()
            .args(["remote", "rename", old, new])
            .exec_check()?;
        Ok(())
    }

    /// Sets or adds a URL for `remote`.
    ///
    /// - `push = true` → modify push URL instead of fetch URL.
    /// - `add  = true` → add an additional URL rather than replacing.
    pub fn set_remote_url(
        &self,
        remote: &str,
        url: &str,
        push: bool,
        add: bool,
    ) -> Result<(), GitError> {
        let mut cmd = self.git();
        cmd.args(["remote", "set-url"]);
        if push {
            cmd.arg("--push");
        }
        if add {
            cmd.arg("--add");
        }
        cmd.args([remote, url]).exec_check()?;
        Ok(())
    }

    // -----------------------------------------------------------------------
    // Stash
    // -----------------------------------------------------------------------

    /// Stashes all working-tree changes (tracked + untracked).
    ///
    /// Returns `true` if there was anything to stash.
    pub fn stash(&self, message: Option<&str>) -> Result<bool, GitError> {
        let mut cmd = self.git();
        cmd.args(["stash", "push", "--include-untracked"]);
        if let Some(msg) = message {
            cmd.args(["-m", msg]);
        }
        let result = cmd.exec_check()?;
        // "No local changes to save" exit 0 with that message
        Ok(!result.stdout.contains("No local changes to save"))
    }

    /// Pops the most recent stash entry.
    pub fn stash_pop(&self) -> Result<(), GitError> {
        self.git().args(["stash", "pop"]).exec_check()?;
        Ok(())
    }

    // -----------------------------------------------------------------------
    // Branches
    // -----------------------------------------------------------------------

    /// Creates a new branch at `start_point` (default: current HEAD).
    pub fn create_branch(&self, name: &str, start_point: Option<&str>) -> Result<(), GitError> {
        let mut cmd = self.git();
        cmd.args(["branch", name]);
        if let Some(sp) = start_point {
            cmd.arg(sp);
        }
        cmd.exec_check()?;
        Ok(())
    }

    /// Checks out `branch`, creating it first if `create` is `true`.
    pub fn checkout(&self, branch: &str, create: bool) -> Result<(), GitError> {
        let mut cmd = self.git();
        cmd.arg("checkout");
        if create {
            cmd.arg("-b");
        }
        cmd.arg(branch).exec_check()?;
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    fn setup_test_repo(path: &Path) {
        std::fs::create_dir_all(path).unwrap();
        Command::new("git")
            .args(["init"])
            .current_dir(path)
            .exec_check()
            .unwrap();
        Command::new("git")
            .args(["config", "user.name", "Test User"])
            .current_dir(path)
            .exec_check()
            .unwrap();
        Command::new("git")
            .args(["config", "user.email", "test@example.com"])
            .current_dir(path)
            .exec_check()
            .unwrap();
    }

    fn make_commit(path: &Path, filename: &str) {
        std::fs::write(path.join(filename), "content").unwrap();
        Command::new("git")
            .args(["add", filename])
            .current_dir(path)
            .exec_check()
            .unwrap();
        Command::new("git")
            .args(["commit", "-m", "test commit"])
            .current_dir(path)
            .exec_check()
            .unwrap();
    }

    #[test]
    fn config_get_set_unset() {
        let dir = tempfile::tempdir().unwrap();
        setup_test_repo(dir.path());
        let repo = Repository::new(dir.path());

        repo.set_config("workflow.test.key", "hello").unwrap();
        assert_eq!(
            repo.get_config("workflow.test.key").unwrap(),
            Some("hello".to_owned())
        );

        repo.unset_config("workflow.test.key").unwrap();
        assert_eq!(repo.get_config("workflow.test.key").unwrap(), None);
    }

    #[test]
    fn is_dirty_reflects_working_tree_state() {
        let dir = tempfile::tempdir().unwrap();
        setup_test_repo(dir.path());
        let repo = Repository::new(dir.path());

        // Empty repo: clean
        assert!(!repo.is_dirty(true).unwrap());

        // Create untracked file
        std::fs::write(dir.path().join("new.txt"), "data").unwrap();
        assert!(repo.is_dirty(true).unwrap());
        assert!(!repo.is_dirty(false).unwrap()); // untracked not counted

        // Commit it
        make_commit(dir.path(), "new.txt");
        assert!(!repo.is_dirty(true).unwrap());

        // Modify tracked file
        std::fs::write(dir.path().join("new.txt"), "modified").unwrap();
        assert!(repo.is_dirty(false).unwrap());
        assert!(repo.is_dirty(true).unwrap());
    }

    #[test]
    fn head_branch_and_resolve_commit() {
        let dir = tempfile::tempdir().unwrap();
        setup_test_repo(dir.path());
        let repo = Repository::new(dir.path());

        // Unborn branch: resolve_commit returns None
        assert_eq!(repo.resolve_commit("HEAD").unwrap(), None);

        make_commit(dir.path(), "f.txt");

        let hash = repo.resolve_commit("HEAD").unwrap().unwrap();
        assert_eq!(hash.len(), 40);

        let branch = repo.head_branch().unwrap().unwrap();
        assert!(!branch.is_empty());
    }

    #[test]
    fn detached_head_returns_none_for_branch() {
        let dir = tempfile::tempdir().unwrap();
        setup_test_repo(dir.path());
        let repo = Repository::new(dir.path());
        make_commit(dir.path(), "f.txt");

        let hash = repo.resolve_commit("HEAD").unwrap().unwrap();
        Command::new("git")
            .args(["checkout", &hash])
            .current_dir(dir.path())
            .exec_check()
            .unwrap();

        assert_eq!(repo.head_branch().unwrap(), None);
    }

    #[test]
    fn remote_operations() {
        let dir = tempfile::tempdir().unwrap();
        setup_test_repo(dir.path());
        let repo = Repository::new(dir.path());

        repo.add_remote("origin", "https://example.com/repo.git")
            .unwrap();

        let remotes = repo.list_remotes().unwrap();
        assert_eq!(remotes, vec!["origin"]);

        repo.set_remote_url("origin", "https://example.com/mirror.git", true, true)
            .unwrap();

        let push_urls = repo.remote_urls("origin", true).unwrap();
        assert!(!push_urls.is_empty());

        repo.rename_remote("origin", "upstream").unwrap();
        let remotes2 = repo.list_remotes().unwrap();
        assert_eq!(remotes2, vec!["upstream"]);
    }
}
