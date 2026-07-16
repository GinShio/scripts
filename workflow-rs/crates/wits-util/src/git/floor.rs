//! The read/ref floor: config, branches, commits, ranges, and the ref plumbing
//! (pushes, branch deletes, and the `review` object-fetch/pin refs). Reads run
//! even under a dry-run; the ref/push mutations are captured so a failure keeps
//! git's own message. See the [module overview](super).

use std::collections::HashMap;
use std::path::PathBuf;

use super::{GitError, Repository};

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

/// One entry of a `diff --name-status` over a range: a file the MR touched, its
/// change kind, and its former path when the change was a rename.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileChange {
    pub path: String,
    pub old_path: Option<String>,
    /// The porcelain status letter — `A`dded, `M`odified, `D`eleted, `R`enamed,
    /// `C`opied. Kept as a char because that is exactly git's own vocabulary.
    pub status: char,
}

impl Repository {
    // -- reads ----------------------------------------------------------------

    /// Read a config value, or `None` when the key is unset.
    pub fn get_config(&self, key: &str) -> Result<Option<String>, GitError> {
        Ok(self.query(&["config", "--get", key]))
    }

    /// Every value of a (possibly multi-valued) config key, exactly as written.
    /// Unlike `git remote get-url`, `git config` does **not** apply
    /// `url.*.insteadOf` rewrites, so this is the lens to use when an idempotent
    /// compare must match the literal declared string (e.g. push URLs).
    pub fn get_config_all(&self, key: &str) -> Vec<String> {
        self.query(&["config", "--get-all", key])
            .map(|s| s.lines().map(str::to_owned).collect())
            .unwrap_or_default()
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

    /// Whether a revision exists — the boolean form of [`rev_parse`](Self::rev_parse).
    pub fn rev_exists(&self, spec: &str) -> bool {
        self.rev_parse(spec).is_some()
    }

    /// Whether the path is a git repository (inside a work tree).
    pub fn is_repo(&self) -> bool {
        self.query(&["rev-parse", "--is-inside-work-tree"])
            .as_deref()
            == Some("true")
    }

    /// Whether the path exists on disk at all — the "is there a checkout here?"
    /// question `update` asks before deciding clone-vs-refresh.
    pub fn exists(&self) -> bool {
        self.path().exists()
    }

    /// The short HEAD commit, or `None` on an unborn branch.
    pub fn head_commit(&self) -> Option<String> {
        self.query(&["rev-parse", "--short", "HEAD"])
    }

    /// Whether the working tree has uncommitted changes (tracked or untracked),
    /// **ignoring submodules**. This is the "would a branch switch or checkout
    /// disturb my work?" question — and a superproject `switch`/`checkout` never
    /// touches a submodule's working tree, so a submodule merely sitting at a
    /// different commit is not work at risk. Counting it would stash (or block a
    /// checkout) on every switch in a repo whose submodules have drifted, for
    /// nothing — the `project`/`build` flow realigns submodules explicitly right
    /// after the switch regardless.
    pub fn is_dirty(&self) -> bool {
        self.query(&["status", "--porcelain", "--ignore-submodules=all"])
            .is_some()
    }

    /// The absolute path of the `.git` directory, the natural home for a tool's
    /// own per-repository state files.
    pub fn git_dir(&self) -> Option<PathBuf> {
        self.query(&["rev-parse", "--absolute-git-dir"])
            .map(PathBuf::from)
    }

    /// The absolute path of the *common* git directory — the main `.git` shared
    /// by every linked worktree. Unlike [`git_dir`](Self::git_dir), this is stable
    /// across worktrees, so per-clone state (the review store) lands in the same
    /// place whether you run from the main checkout or a `checkout` worktree.
    pub fn git_common_dir(&self) -> Option<PathBuf> {
        self.query(&["rev-parse", "--path-format=absolute", "--git-common-dir"])
            .map(PathBuf::from)
    }

    /// The working tree's top-level directory, or `None` outside a work tree
    /// (e.g. a bare repo). The natural anchor for deriving a sibling worktree.
    pub fn toplevel(&self) -> Option<PathBuf> {
        self.query(&["rev-parse", "--show-toplevel"])
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

    /// The files a range (`base..head`) touched, rename-aware. Empty when the
    /// range can't be computed (e.g. the base object isn't present locally),
    /// which the caller treats as "unknown" rather than "nothing changed".
    pub fn changed_files(&self, range: &str) -> Vec<FileChange> {
        let Some(out) = self.query(&["diff", "--name-status", "-M", range]) else {
            return Vec::new();
        };
        out.lines()
            .filter_map(|line| {
                let mut fields = line.split('\t');
                let status = fields.next()?.chars().next()?;
                match status {
                    'R' | 'C' => {
                        let old = fields.next()?.to_owned();
                        let new = fields.next()?.to_owned();
                        Some(FileChange {
                            path: new,
                            old_path: Some(old),
                            status,
                        })
                    }
                    _ => Some(FileChange {
                        path: fields.next()?.to_owned(),
                        old_path: None,
                        status,
                    }),
                }
            })
            .collect()
    }

    /// A textual diff for a range, optionally narrowed to one path — the
    /// `diff --patch` convenience for a terminal or for debugging, never the
    /// editor's render path.
    pub fn diff_patch(&self, range: &str, path: Option<&str>) -> Option<String> {
        let mut args = vec!["diff", range];
        if let Some(p) = path {
            args.push("--");
            args.push(p);
        }
        self.query(&args)
    }

    // -- ref & history mutations (captured) -----------------------------------

    /// Force-push a branch to a remote, but refuse to overwrite commits the
    /// remote has that we don't (`--force-with-lease`). History-editing
    /// workflows make non-fast-forward pushes routine, so a plain force is the
    /// reflex — yet plain force will happily discard a push someone else made.
    /// The lease keeps the legitimate "I rewrote my own branch" case working
    /// while failing closed when the remote moved underneath us. Mutating, so
    /// dry-run prints rather than pushes.
    pub fn push_force_with_lease(&self, remote: &str, branch: &str) -> Result<(), GitError> {
        self.capture(
            format!("push {branch} -> {remote}"),
            &["push", remote, branch, "--force-with-lease"],
            false,
        )
    }

    /// Delete a local branch. Without `force` this is `git branch -d`, which
    /// refuses to drop a branch that isn't merged — the safety we want by
    /// default; `force` escalates to `-D`. Mutating, so dry-run prints.
    pub fn delete_branch(&self, name: &str, force: bool) -> Result<(), GitError> {
        let flag = if force { "-D" } else { "-d" };
        self.capture(
            format!("delete branch {name}"),
            &["branch", flag, name],
            false,
        )
    }

    // The `review` acquisition refs: fetch an MR's objects and hold them alive
    // with our own `refs/wits/review/*` pins. These run even under dry-run (like
    // every other read) — pinning a ref is local bookkeeping, not a change to the
    // remote or the user's branches.

    /// Fetch a remote ref into a local ref, forcing the update. Used to pull an
    /// MR head (`refs/pull/<n>/head`) into a `refs/wits/review/*` pin.
    pub fn fetch_ref(
        &self,
        remote: &str,
        remote_ref: &str,
        local_ref: &str,
    ) -> Result<(), GitError> {
        self.capture(
            format!("fetch {remote_ref} from {remote}"),
            &[
                "fetch",
                "--no-tags",
                remote,
                &format!("+{remote_ref}:{local_ref}"),
            ],
            true,
        )
    }

    /// Best-effort fetch of a bare object (e.g. an MR's base SHA, which may not
    /// be an ancestor of the head we already pulled) into a local ref. Servers
    /// that forbid fetching an arbitrary SHA make this fail; that is fine — the
    /// caller treats the object as simply unavailable.
    pub fn try_fetch_object(&self, remote: &str, sha: &str, local_ref: &str) -> bool {
        self.capture(
            format!("fetch object {sha}"),
            &["fetch", "--no-tags", remote, &format!("+{sha}:{local_ref}")],
            true,
        )
        .is_ok()
    }

    /// Point a ref at an object (our own `refs/wits/review/*` bookkeeping).
    pub fn update_ref(&self, name: &str, target: &str) -> Result<(), GitError> {
        self.capture(
            format!("update-ref {name}"),
            &["update-ref", name, target],
            true,
        )
    }

    /// Delete a ref. Mutating on purpose — this is `prune`'s cleanup, which a
    /// `-n` run should preview rather than perform.
    pub fn delete_ref(&self, name: &str) -> Result<(), GitError> {
        self.capture(
            format!("delete ref {name}"),
            &["update-ref", "-d", name],
            false,
        )
    }

    /// Every ref under `prefix` (e.g. `refs/wits/review/`) mapped to its target
    /// object id. The record of which snapshots we have pinned.
    pub fn refs_under(&self, prefix: &str) -> Vec<(String, String)> {
        let Some(out) = self.query(&["for-each-ref", "--format=%(refname) %(objectname)", prefix])
        else {
            return Vec::new();
        };
        out.lines()
            .filter_map(|line| line.split_once(' '))
            .map(|(name, oid)| (name.to_owned(), oid.to_owned()))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::process::Command;

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
        let _guard = crate::log::test_flag_guard();
        let dir = init_repo();
        Command::new("git")
            .args(["config", "wits.transcrypt.password", "hunter2"])
            .current_dir(dir.path())
            .force_run()
            .exec()
            .unwrap();

        let repo = Repository::new(dir.path());
        assert_eq!(
            repo.get_config("wits.transcrypt.password").unwrap(),
            Some("hunter2".to_owned())
        );
        assert_eq!(repo.get_config("wits.transcrypt.absent").unwrap(), None);
    }

    #[test]
    fn reports_current_branch_and_branch_tips() {
        let _guard = crate::log::test_flag_guard();
        let dir = init_repo();
        let repo = Repository::new(dir.path());

        assert_eq!(repo.current_branch().as_deref(), Some("main"));
        let tips = repo.branch_tips();
        assert!(tips.contains_key("main"));
        assert_eq!(tips["main"], repo.rev_parse("main").unwrap());
    }

    #[test]
    fn commits_split_subject_and_body_oldest_first() {
        let _guard = crate::log::test_flag_guard();
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

    #[test]
    fn dirty_tracks_superproject_changes() {
        let _guard = crate::log::test_flag_guard();
        let dir = init_repo();
        let repo = Repository::new(dir.path());
        assert!(!repo.is_dirty(), "a fresh committed tree is clean");
        std::fs::write(dir.path().join("scratch.txt"), "x").unwrap();
        assert!(repo.is_dirty(), "an untracked file makes it dirty");
    }
}
