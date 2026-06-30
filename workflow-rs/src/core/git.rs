//! Talking to git by shelling out to the `git` binary.
//!
//! The obvious alternative is linking libgit2, and for a tool that mostly reads
//! config and refs that seems heavier than warranted. The deciding factor isn't
//! weight though — it's fidelity. A user's real git behaviour is the sum of
//! their `~/.gitconfig` includes, conditional includes, credential helpers, SSH
//! config and agent forwarding. libgit2 reimplements a subset of that and drifts
//! from the CLI in exactly the corners (includes, helpers) that config and
//! remote resolution care about. Driving the same `git` the user's shell does
//! means we read precisely what they would, with no second implementation to
//! keep in sync. The cost is a process spawn per query, which is nothing next to
//! the network round-trips these tools spend most of their time on.
//!
//! The surface grows strictly with what the commands need. Reads opt into
//! [`force_run`](crate::core::process::Command::force_run) so they still answer
//! during a dry-run — control flow depends on them, and a dry-run that can't
//! read the world tells you nothing.

use std::collections::HashMap;
use std::path::PathBuf;

use thiserror::Error;

use crate::core::process::{Command, ProcessError};

#[derive(Debug, Error)]
pub enum GitError {
    #[error("git command failed: {0}")]
    Process(#[from] ProcessError),
    #[error("git {operation} failed: {message}")]
    Failed { operation: String, message: String },
}

/// A commit's identity and its message pre-split into subject (first line) and
/// body. The split lives here because the two are almost always wanted
/// separately — a short summary versus the detail — and doing it once avoids
/// every caller re-deriving the same boundary.
#[derive(Debug, Clone)]
pub struct Commit {
    pub hash: String,
    pub subject: String,
    pub body: String,
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

    /// Run a read-only git query and return its trimmed stdout, or `None` when
    /// the command exits non-zero or prints nothing. The whole read surface is
    /// built on this, so the dry-run/force decision lives in exactly one place.
    fn query(&self, args: &[&str]) -> Option<String> {
        let result = self
            .git()
            .args(args.iter().copied())
            .force_run()
            .exec()
            .ok()?;
        if result.is_success() {
            let out = result.stdout_trimmed();
            if out.is_empty() {
                None
            } else {
                Some(out.to_owned())
            }
        } else {
            None
        }
    }

    /// Read a config value, or `None` when the key is unset.
    pub fn get_config(&self, key: &str) -> Result<Option<String>, GitError> {
        Ok(self.query(&["config", "--get", key]))
    }

    /// The branch currently checked out, or `None` on a detached HEAD. A
    /// detached HEAD has no name to push or build on, so the absence is
    /// meaningful rather than an error.
    pub fn current_branch(&self) -> Option<String> {
        self.query(&["symbolic-ref", "--quiet", "--short", "HEAD"])
    }

    /// Resolve a revision to a full commit hash, or `None` if it doesn't exist.
    pub fn rev_parse(&self, spec: &str) -> Option<String> {
        self.query(&["rev-parse", "--verify", "--quiet", spec])
    }

    /// The absolute path of the `.git` directory, the natural home for a tool's
    /// own per-repository state files.
    pub fn git_dir(&self) -> Option<PathBuf> {
        self.query(&["rev-parse", "--absolute-git-dir"])
            .map(PathBuf::from)
    }

    /// The (fetch) URL of a named remote, or `None` if the remote is absent.
    pub fn remote_url(&self, name: &str) -> Option<String> {
        self.query(&["remote", "get-url", name])
    }

    /// Every local branch mapped to its tip commit. This is the content
    /// source-of-truth: whatever a branch points at here is what gets pushed.
    pub fn branch_tips(&self) -> HashMap<String, String> {
        let mut map = HashMap::new();
        let Some(out) = self.query(&[
            "for-each-ref",
            "--format=%(refname:short) %(objectname)",
            "refs/heads",
        ]) else {
            return map;
        };
        for line in out.lines() {
            if let Some((name, oid)) = line.split_once(' ') {
                map.insert(name.to_owned(), oid.to_owned());
            }
        }
        map
    }

    /// The default branch a remote points its HEAD at (e.g. `main`), read from
    /// the locally-tracked `refs/remotes/<remote>/HEAD` symref. Returns `None`
    /// when that symref hasn't been established (a fresh clone may lack it until
    /// `git remote set-head`), letting the caller fall through to its next guess.
    pub fn remote_default_branch(&self, remote: &str) -> Option<String> {
        let symref = format!("refs/remotes/{remote}/HEAD");
        let target = self.query(&["symbolic-ref", "--quiet", &symref])?;
        let prefix = format!("refs/remotes/{remote}/");
        target.strip_prefix(&prefix).map(str::to_owned)
    }

    /// Commits in `range` (e.g. `main..feature`), oldest first.
    ///
    /// Subject and body are separated with control characters rather than
    /// newlines because a commit body is itself multi-line; the unit/record
    /// separators (`0x1f`/`0x1e`) can't occur in a message, so parsing stays
    /// unambiguous no matter how the author formatted things.
    pub fn commits(&self, range: &str) -> Vec<Commit> {
        let Some(out) = self.query(&[
            "log",
            "--reverse",
            "--pretty=format:%H%x1f%s%x1f%b%x1e",
            range,
        ]) else {
            return Vec::new();
        };

        out.split('\u{1e}')
            .map(str::trim)
            .filter(|record| !record.is_empty())
            .filter_map(|record| {
                let mut fields = record.splitn(3, '\u{1f}');
                let hash = fields.next()?.trim().to_owned();
                let subject = fields.next().unwrap_or("").trim().to_owned();
                let body = fields.next().unwrap_or("").trim().to_owned();
                Some(Commit {
                    hash,
                    subject,
                    body,
                })
            })
            .collect()
    }

    /// Force-push a branch to a remote, but refuse to overwrite commits the
    /// remote has that we don't (`--force-with-lease`). History-editing
    /// workflows make non-fast-forward pushes routine, so a plain force is the
    /// reflex — yet plain force will happily discard a push someone else made.
    /// The lease keeps the legitimate "I rewrote my own branch" case working
    /// while failing closed when the remote moved underneath us. Mutating, so
    /// dry-run prints rather than pushes.
    pub fn push_force_with_lease(&self, remote: &str, branch: &str) -> Result<(), GitError> {
        let result = self
            .git()
            .args(["push", remote, branch, "--force-with-lease"])
            .exec()?;
        if result.is_success() {
            Ok(())
        } else {
            Err(GitError::Failed {
                operation: format!("push {branch} -> {remote}"),
                message: result.stderr.trim().to_owned(),
            })
        }
    }

    /// Delete a local branch. Without `force` this is `git branch -d`, which
    /// refuses to drop a branch that isn't merged — the safety we want by
    /// default; `force` escalates to `-D`. Mutating, so dry-run prints.
    pub fn delete_branch(&self, name: &str, force: bool) -> Result<(), GitError> {
        let flag = if force { "-D" } else { "-d" };
        let result = self.git().args(["branch", flag, name]).exec()?;
        if result.is_success() {
            Ok(())
        } else {
            Err(GitError::Failed {
                operation: format!("delete branch {name}"),
                message: result.stderr.trim().to_owned(),
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::process::Command;

    /// Spin up a throwaway repo with one commit on `main` so ref/commit reads
    /// have something real to look at. `force_run` because tests share the
    /// global dry-run flag and run in parallel.
    fn init_repo() -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        let run = |args: &[&str]| {
            Command::new("git")
                .args(args.iter().copied())
                .current_dir(dir.path())
                .force_run()
                .exec()
                .unwrap();
        };
        run(&["init", "-b", "main"]);
        run(&["config", "user.email", "t@example.com"]);
        run(&["config", "user.name", "Test"]);
        run(&["commit", "--allow-empty", "-m", "root"]);
        dir
    }

    #[test]
    fn reads_a_set_value_and_reports_missing_as_none() {
        let _guard = crate::core::log::test_flag_guard();
        let dir = init_repo();
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

    #[test]
    fn reports_current_branch_and_branch_tips() {
        let _guard = crate::core::log::test_flag_guard();
        let dir = init_repo();
        let repo = Repository::new(dir.path());

        assert_eq!(repo.current_branch().as_deref(), Some("main"));
        let tips = repo.branch_tips();
        assert!(tips.contains_key("main"));
        assert_eq!(tips["main"], repo.rev_parse("main").unwrap());
    }

    #[test]
    fn commits_split_subject_and_body_oldest_first() {
        let _guard = crate::core::log::test_flag_guard();
        let dir = init_repo();
        let run = |args: &[&str]| {
            Command::new("git")
                .args(args.iter().copied())
                .current_dir(dir.path())
                .force_run()
                .exec()
                .unwrap();
        };
        run(&[
            "commit",
            "--allow-empty",
            "-m",
            "first subject\n\nfirst body line",
        ]);
        run(&["commit", "--allow-empty", "-m", "second subject"]);

        let repo = Repository::new(dir.path());
        let commits = repo.commits("main~2..main");
        assert_eq!(commits.len(), 2);
        assert_eq!(commits[0].subject, "first subject");
        assert_eq!(commits[0].body, "first body line");
        assert_eq!(commits[1].subject, "second subject");
        assert_eq!(commits[1].body, "");
    }
}
