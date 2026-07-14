//! The working-tree porcelain: worktrees, stashes, submodules, branch switches,
//! sparse cones, and clone — the wide, mutation-heavy surface the
//! `project`/`build`/`update` actions drive. Its mutations stream to the
//! terminal so progress shows live; its reads answer even under dry-run. See the
//! [module overview](super).

use std::path::Path;

use super::{GitError, Repository};
use crate::process::Command;

/// One `git worktree list` entry.
pub struct Worktree {
    pub path: std::path::PathBuf,
    pub branch: Option<String>,
}

impl Repository {
    // -- working-tree reads ---------------------------------------------------

    /// Submodule paths recorded in `.gitmodules`, restricted to those that are
    /// materialised on disk (a sparse checkout may omit some).
    pub fn materialised_submodules(&self) -> Vec<String> {
        let Some(out) = self.query(&["config", "--file", ".gitmodules", "--get-regexp", "path"])
        else {
            return Vec::new();
        };
        out.lines()
            .filter_map(|line| line.split_once(' ').map(|(_, p)| p.trim().to_owned()))
            .filter(|p| self.path().join(p).exists())
            .collect()
    }

    pub fn worktrees(&self) -> Vec<Worktree> {
        let Some(out) = self.query(&["worktree", "list", "--porcelain"]) else {
            return Vec::new();
        };
        let mut result = Vec::new();
        let mut path: Option<std::path::PathBuf> = None;
        let mut branch: Option<String> = None;
        for line in out.lines() {
            if let Some(p) = line.strip_prefix("worktree ") {
                if let Some(prev) = path.take() {
                    result.push(Worktree {
                        path: prev,
                        branch: branch.take(),
                    });
                }
                path = Some(std::path::PathBuf::from(p));
                branch = None;
            } else if let Some(b) = line.strip_prefix("branch ") {
                branch = Some(b.trim_start_matches("refs/heads/").to_owned());
            }
        }
        if let Some(p) = path {
            result.push(Worktree { path: p, branch });
        }
        result
    }

    /// Is `sparse-checkout` active for this checkout?
    pub fn is_sparse(&self) -> bool {
        self.query(&["config", "--bool", "core.sparseCheckout"])
            .as_deref()
            == Some("true")
    }

    /// The active sparse-checkout patterns (empty if not sparse).
    pub fn sparse_list(&self) -> Vec<String> {
        self.query(&["sparse-checkout", "list"])
            .map(|s| s.lines().map(str::to_owned).collect())
            .unwrap_or_default()
    }

    // -- working-tree mutations (streamed) ------------------------------------

    pub fn switch(&self, branch: &str) -> Result<(), GitError> {
        self.stream(&format!("switch to {branch}"), &["switch", branch])
    }

    /// Stash the working tree (including untracked). Returns whether anything was
    /// stashed, so a caller only pops when it pushed.
    pub fn stash_push(&self, message: &str) -> Result<bool, GitError> {
        if !self.is_dirty() {
            return Ok(false);
        }
        self.stream(
            "stash",
            &["stash", "push", "--include-untracked", "--message", message],
        )?;
        Ok(true)
    }

    pub fn stash_pop(&self) -> Result<(), GitError> {
        self.stream("stash pop", &["stash", "pop"])
    }

    pub fn fetch(&self, args: &[&str]) -> Result<(), GitError> {
        let mut all = vec!["fetch"];
        all.extend_from_slice(args);
        self.stream("fetch", &all)
    }

    pub fn merge_ff_only(&self, rev: &str) -> Result<(), GitError> {
        self.stream(
            &format!("fast-forward to {rev}"),
            &["merge", "--ff-only", rev],
        )
    }

    pub fn ensure_remote(&self, name: &str, url: &str) -> Result<(), GitError> {
        if self.remote_url(name).is_none() {
            self.stream(&format!("add remote {name}"), &["remote", "add", name, url])?;
        }
        Ok(())
    }

    pub fn ensure_push_url(&self, name: &str, url: &str) -> Result<(), GitError> {
        // Compare against the *raw* configured push URLs (`git config`), never
        // `git remote get-url`, whose output is rewritten by `url.*.insteadOf`.
        // An exact-string guard on the rewritten form never matches the declared
        // URL, so every run re-`--add`s it — the runaway pile of push URLs.
        let configured = self.get_config_all(&format!("remote.{name}.pushurl"));
        if !configured.iter().any(|u| u == url) {
            self.stream(
                &format!("add push url to {name}"),
                &["remote", "set-url", "--add", "--push", name, url],
            )?;
        }
        Ok(())
    }

    pub fn submodule_update(&self, paths: &[String], init: bool) -> Result<(), GitError> {
        if paths.is_empty() {
            return Ok(());
        }
        let mut args = vec!["submodule", "update", "--recursive"];
        if init {
            args.push("--init");
        }
        args.push("--");
        let path_refs: Vec<&str> = paths.iter().map(String::as_str).collect();
        args.extend(path_refs);
        self.stream("submodule update", &args)
    }

    pub fn worktree_add(
        &self,
        dir: &Path,
        branch: &str,
        no_checkout: bool,
    ) -> Result<(), GitError> {
        let dir_s = dir.display().to_string();
        let mut args = vec!["worktree", "add"];
        if no_checkout {
            args.push("--no-checkout");
        }
        args.push(&dir_s);
        args.push(branch);
        self.stream(&format!("add worktree for {branch}"), &args)
    }

    pub fn worktree_remove(&self, dir: &Path, force: bool) -> Result<(), GitError> {
        let dir_s = dir.display().to_string();
        let mut args = vec!["worktree", "remove"];
        if force {
            args.push("--force");
        }
        args.push(&dir_s);
        self.stream("remove worktree", &args)
    }

    pub fn checkout(&self, rev: &str) -> Result<(), GitError> {
        self.stream(&format!("checkout {rev}"), &["checkout", rev])
    }

    pub fn sparse_set(&self, patterns: &[String]) -> Result<(), GitError> {
        let mut args = vec!["sparse-checkout", "set"];
        let refs: Vec<&str> = patterns.iter().map(String::as_str).collect();
        args.extend(refs);
        self.stream("set sparse-checkout", &args)
    }

    /// Populate the working tree from HEAD (used after a `--no-checkout` worktree
    /// add once sparse patterns are in place).
    pub fn checkout_head(&self) -> Result<(), GitError> {
        self.stream("checkout HEAD", &["checkout", "HEAD"])
    }
}

/// Restores a repo to the branch (and stash) it was on when captured, on *any*
/// scope exit — success, `?`-propagated error, or panic. This is the RAII form
/// of the classic stash → switch → build → switch back → pop dance: correctness
/// no longer depends on remembering to restore on every path. Restore is
/// best-effort and logs (Drop cannot return errors), which is the right failure
/// mode — a failed restore should warn, not mask the original error.
pub struct RestoreGuard<'a> {
    repo: &'a Repository,
    original_branch: Option<String>,
    stashed: bool,
}

impl<'a> RestoreGuard<'a> {
    /// Capture the current branch as the state to return to.
    pub fn capture(repo: &'a Repository) -> Self {
        RestoreGuard {
            repo,
            original_branch: repo.current_branch(),
            stashed: false,
        }
    }

    pub fn mark_stashed(&mut self) {
        self.stashed = true;
    }
}

impl Drop for RestoreGuard<'_> {
    fn drop(&mut self) {
        if let Some(orig) = &self.original_branch {
            if self.repo.current_branch().as_deref() != Some(orig.as_str()) {
                if let Err(e) = self.repo.switch(orig) {
                    log::warn!("could not restore branch {orig}: {e}");
                }
            }
        }
        if self.stashed {
            if let Err(e) = self.repo.stash_pop() {
                log::warn!("could not pop auto-stash: {e}");
            }
        }
    }
}

/// Clone `url` into `dir`, naming the fetched remote `remote`. A free function
/// because there is no repository yet to hang it off. `--origin` lets a repo
/// tracked from `upstream` leave the `origin` name free for a fork that may not
/// exist on the server yet.
pub fn clone(url: &str, remote: &str, dir: &Path) -> Result<(), GitError> {
    // Inherit stdio so clone progress streams live and in colour.
    let dir_s = dir.display().to_string();
    let code = Command::new("git")
        .args(["clone", "--origin", remote, url, &dir_s])
        .status()?;
    if code == 0 {
        Ok(())
    } else {
        Err(GitError::Failed {
            operation: format!("clone {url}"),
            message: format!("exit {code}"),
        })
    }
}
