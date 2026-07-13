//! Git operations the `project` actions need, driven through the `git` CLI.
//!
//! This lives beside the command rather than in `core::git` on purpose: it is a
//! wide, mutation-heavy, project-specific surface (worktrees, submodules, stash
//! dances, remote reconciliation), and `core` is kept to the floor. Reads opt
//! into `force_run` so a dry-run still introspects the world; mutations do not,
//! so `-n` prints them instead of running them.

use std::path::{Path, PathBuf};

use anyhow::{bail, Result};

use crate::process::Command;

pub struct Git {
    dir: PathBuf,
}

/// One `git worktree list` entry.
pub struct Worktree {
    pub path: PathBuf,
    pub branch: Option<String>,
}

impl Git {
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Git { dir: dir.into() }
    }

    pub fn path(&self) -> &Path {
        &self.dir
    }

    fn git(&self) -> Command {
        let mut c = Command::new("git");
        c.current_dir(&self.dir);
        c
    }

    /// A read-only query: trimmed stdout, or `None` on failure/empty. Runs even
    /// under dry-run.
    fn query(&self, args: &[&str]) -> Option<String> {
        let out = self
            .git()
            .args(args.iter().copied())
            .force_run()
            .exec()
            .ok()?;
        if out.is_success() {
            let s = out.stdout_trimmed();
            (!s.is_empty()).then(|| s.to_owned())
        } else {
            None
        }
    }

    /// A mutation: printed under dry-run, otherwise run with inherited stdio so
    /// its progress streams live and in colour (git detects the tty). The child
    /// prints its own error to stderr, so a non-zero exit needs only a terse tag.
    fn run(&self, note: &str, args: &[&str]) -> Result<()> {
        let code = self.git().args(args.iter().copied()).status()?;
        if code == 0 {
            Ok(())
        } else {
            bail!("{note} failed (exit {code})");
        }
    }

    // --- reads ---------------------------------------------------------------

    pub fn exists(&self) -> bool {
        self.dir.exists()
    }

    pub fn is_repo(&self) -> bool {
        self.query(&["rev-parse", "--is-inside-work-tree"])
            .as_deref()
            == Some("true")
    }

    pub fn current_branch(&self) -> Option<String> {
        self.query(&["symbolic-ref", "--quiet", "--short", "HEAD"])
    }

    pub fn head_commit(&self) -> Option<String> {
        self.query(&["rev-parse", "--short", "HEAD"])
    }

    pub fn rev_exists(&self, rev: &str) -> bool {
        self.query(&["rev-parse", "--verify", "--quiet", rev])
            .is_some()
    }

    pub fn is_dirty(&self) -> bool {
        self.query(&["status", "--porcelain"]).is_some()
    }

    pub fn remote_url(&self, name: &str) -> Option<String> {
        self.query(&["remote", "get-url", name])
    }

    pub fn push_urls(&self, name: &str) -> Vec<String> {
        self.query(&["remote", "get-url", "--push", "--all", name])
            .map(|s| s.lines().map(str::to_owned).collect())
            .unwrap_or_default()
    }

    /// Submodule paths recorded in `.gitmodules`, restricted to those that are
    /// materialised on disk (a sparse checkout may omit some).
    pub fn materialised_submodules(&self) -> Vec<String> {
        let Some(out) = self.query(&["config", "--file", ".gitmodules", "--get-regexp", "path"])
        else {
            return Vec::new();
        };
        out.lines()
            .filter_map(|line| line.split_once(' ').map(|(_, p)| p.trim().to_owned()))
            .filter(|p| self.dir.join(p).exists())
            .collect()
    }

    pub fn worktrees(&self) -> Vec<Worktree> {
        let Some(out) = self.query(&["worktree", "list", "--porcelain"]) else {
            return Vec::new();
        };
        let mut result = Vec::new();
        let mut path: Option<PathBuf> = None;
        let mut branch: Option<String> = None;
        for line in out.lines() {
            if let Some(p) = line.strip_prefix("worktree ") {
                if let Some(prev) = path.take() {
                    result.push(Worktree {
                        path: prev,
                        branch: branch.take(),
                    });
                }
                path = Some(PathBuf::from(p));
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

    // --- mutations -----------------------------------------------------------

    pub fn switch(&self, branch: &str) -> Result<()> {
        self.run(&format!("switch to {branch}"), &["switch", branch])
    }

    /// Stash the working tree (including untracked). Returns whether anything was
    /// stashed, so a caller only pops when it pushed.
    pub fn stash_push(&self, message: &str) -> Result<bool> {
        if !self.is_dirty() {
            return Ok(false);
        }
        self.run(
            "stash",
            &["stash", "push", "--include-untracked", "--message", message],
        )?;
        Ok(true)
    }

    pub fn stash_pop(&self) -> Result<()> {
        self.run("stash pop", &["stash", "pop"])
    }

    pub fn fetch(&self, args: &[&str]) -> Result<()> {
        let mut all = vec!["fetch"];
        all.extend_from_slice(args);
        self.run("fetch", &all)
    }

    pub fn merge_ff_only(&self, rev: &str) -> Result<()> {
        self.run(
            &format!("fast-forward to {rev}"),
            &["merge", "--ff-only", rev],
        )
    }

    pub fn ensure_remote(&self, name: &str, url: &str) -> Result<()> {
        if self.remote_url(name).is_none() {
            self.run(&format!("add remote {name}"), &["remote", "add", name, url])?;
        }
        Ok(())
    }

    pub fn ensure_push_url(&self, name: &str, url: &str) -> Result<()> {
        if !self.push_urls(name).iter().any(|u| u == url) {
            self.run(
                &format!("add push url to {name}"),
                &["remote", "set-url", "--add", "--push", name, url],
            )?;
        }
        Ok(())
    }

    pub fn submodule_update(&self, paths: &[String], init: bool) -> Result<()> {
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
        self.run("submodule update", &args)
    }

    pub fn worktree_add(&self, dir: &Path, branch: &str, no_checkout: bool) -> Result<()> {
        let dir_s = dir.display().to_string();
        let mut args = vec!["worktree", "add"];
        if no_checkout {
            args.push("--no-checkout");
        }
        args.push(&dir_s);
        args.push(branch);
        self.run(&format!("add worktree for {branch}"), &args)
    }

    pub fn worktree_remove(&self, dir: &Path, force: bool) -> Result<()> {
        let dir_s = dir.display().to_string();
        let mut args = vec!["worktree", "remove"];
        if force {
            args.push("--force");
        }
        args.push(&dir_s);
        self.run("remove worktree", &args)
    }

    pub fn checkout(&self, rev: &str) -> Result<()> {
        self.run(&format!("checkout {rev}"), &["checkout", rev])
    }

    /// The active sparse-checkout patterns (empty if not sparse).
    pub fn sparse_list(&self) -> Vec<String> {
        self.query(&["sparse-checkout", "list"])
            .map(|s| s.lines().map(str::to_owned).collect())
            .unwrap_or_default()
    }

    pub fn sparse_set(&self, patterns: &[String]) -> Result<()> {
        let mut args = vec!["sparse-checkout", "set"];
        let refs: Vec<&str> = patterns.iter().map(String::as_str).collect();
        args.extend(refs);
        self.run("set sparse-checkout", &args)
    }

    /// Populate the working tree from HEAD (used after a `--no-checkout` worktree
    /// add once sparse patterns are in place).
    pub fn checkout_head(&self) -> Result<()> {
        self.run("checkout HEAD", &["checkout", "HEAD"])
    }
}

/// Restores a repo to the branch (and stash) it was on when captured, on *any*
/// scope exit — success, `?`-propagated error, or panic. This is the RAII form
/// of the classic stash → switch → build → switch back → pop dance: correctness
/// no longer depends on remembering to restore on every path. Restore is
/// best-effort and logs (Drop cannot return errors), which is the right failure
/// mode — a failed restore should warn, not mask the original error.
pub struct RestoreGuard<'a> {
    git: &'a Git,
    original_branch: Option<String>,
    stashed: bool,
}

impl<'a> RestoreGuard<'a> {
    /// Capture the current branch as the state to return to.
    pub fn capture(git: &'a Git) -> Self {
        RestoreGuard {
            git,
            original_branch: git.current_branch(),
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
            if self.git.current_branch().as_deref() != Some(orig.as_str()) {
                if let Err(e) = self.git.switch(orig) {
                    log::warn!("could not restore branch {orig}: {e}");
                }
            }
        }
        if self.stashed {
            if let Err(e) = self.git.stash_pop() {
                log::warn!("could not pop auto-stash: {e}");
            }
        }
    }
}

/// Clone `url` into `dir`, naming the fetched remote `remote`. A free function
/// because there is no repository yet to hang it off. `--origin` lets a repo
/// tracked from `upstream` leave the `origin` name free for a fork that may not
/// exist on the server yet.
pub fn clone(url: &str, remote: &str, dir: &Path) -> Result<()> {
    // Inherit stdio so clone progress streams live and in colour.
    let dir_s = dir.display().to_string();
    let code = Command::new("git")
        .args(["clone", "--origin", remote, url, &dir_s])
        .status()?;
    if code == 0 {
        Ok(())
    } else {
        bail!("clone {url} failed (exit {code})");
    }
}
