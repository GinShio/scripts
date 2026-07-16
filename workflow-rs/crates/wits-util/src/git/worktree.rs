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

    /// The main worktree's *working tree* — the directory `review checkout`
    /// anchors its sibling `.review/` dir to, stable no matter which worktree we
    /// are invoked from.
    ///
    /// `git worktree list` is no help here: for a repository that is itself a
    /// **submodule**, it reports that repo's main worktree as its *git-dir*
    /// (`<super>/.git/modules/<name>`), not its working tree — anchoring there
    /// would bury the review worktree inside `.git/modules`. Instead:
    ///
    /// - in the **main** worktree (`--git-dir` == `--git-common-dir`) the working
    ///   tree is exactly `--show-toplevel`, correct for a normal repo *and* a
    ///   submodule (whose working tree lives outside its git-dir);
    /// - in a **linked** worktree the main worktree's working tree is derived
    ///   from the common git-dir: a submodule records it as `core.worktree`
    ///   (relative to the common dir), and a normal repo leaves it unset, where
    ///   the working tree is the parent of `<main>/.git`.
    pub fn main_worktree(&self) -> Option<std::path::PathBuf> {
        let common = self.git_common_dir()?;
        if self.git_dir().as_ref() == Some(&common) {
            return self.toplevel();
        }
        // A linked worktree: never anchor off the *current* toplevel (that is
        // what makes review worktrees nest under one another), nor off git
        // worktree list (a git-dir for a submodule). The common config's
        // `core.worktree` points at the main working tree for a submodule; a
        // normal repo has none, so `<main>/.git` → `<main>`.
        match self.config_file_get(&common.join("config"), "core.worktree") {
            Some(worktree) => Some(normalize_path(common.join(worktree))),
            None => common.parent().map(std::path::Path::to_path_buf),
        }
    }

    /// Read a single value from an *explicit* config file rather than the repo's
    /// resolved config — the way to reach the **common** config from inside a
    /// linked worktree (whose own resolved config may shadow it).
    fn config_file_get(&self, file: &std::path::Path, key: &str) -> Option<String> {
        let file = file.to_string_lossy();
        self.query(&["config", "--file", &file, "--get", key])
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

    /// Init-and-update a submodule **and its whole nested tree**, borrowing
    /// objects from `reference` (git alternates — no re-download) at *every*
    /// level. A linked worktree does *not* share the primary's submodule object
    /// store, so without this each `submodule update` re-clones in full.
    ///
    /// The borrowing is unconditional and self-completing:
    /// - `--reference <store>` aims the *top* submodule's clone at the primary's
    ///   store for it;
    /// - `submodule.alternateLocation=superproject` **chains** that down — each
    ///   nested submodule derives its own alternate from its superproject's, so a
    ///   deep tree borrows the *correct* store at every level (a lone
    ///   `--reference` would instead aim every level at the top store, and the
    ///   deeper ones would download);
    /// - `submodule.alternateErrorStrategy=info` degrades a *missing* store to a
    ///   note plus a normal fetch rather than an error, so a level the primary
    ///   never initialised simply downloads — the graceful fallback, baked in.
    ///
    /// Never shallow: borrowing removes the size pressure that would motivate
    /// `--depth`, which only buys fragility (a shallow boundary, server-dependent
    /// arbitrary-SHA fetches, a broken `git describe`).
    ///
    /// One call per *direct* submodule: `--reference` is a single real repository
    /// (git rejects a base directory) and each direct submodule has its own
    /// store, so a caller with several loops, computing each one's `<store>`; the
    /// nested levels beneath each are then handled by the chaining above.
    pub fn submodule_init_borrow(
        &self,
        path: &str,
        reference: Option<&Path>,
    ) -> Result<(), GitError> {
        let mut args: Vec<String> = vec![
            "-c".into(),
            "submodule.alternateLocation=superproject".into(),
            "-c".into(),
            "submodule.alternateErrorStrategy=info".into(),
            "submodule".into(),
            "update".into(),
            "--init".into(),
            "--recursive".into(),
        ];
        if let Some(r) = reference {
            args.push("--reference".into());
            args.push(r.display().to_string());
        }
        args.push("--".into());
        args.push(path.to_owned());
        let arg_refs: Vec<&str> = args.iter().map(String::as_str).collect();
        self.stream(&format!("submodule init {path}"), &arg_refs)
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

#[cfg(test)]
mod tests {
    use super::*;

    /// Borrowing a submodule's objects from a reference store must leave the new
    /// clone with an `alternates` file pointing at that store — the "no
    /// re-download" guarantee `review checkout --submodules` relies on.
    #[test]
    fn submodule_init_borrows_objects_via_alternates() {
        let _guard = crate::log::test_flag_guard();
        // The submodule clone `submodule_init_borrow` triggers is a *child* git
        // process, which inherits repo config only via the environment — so the
        // file-protocol allowance (needed for a local test submodule; real ones
        // are https/ssh) and identity go through `GIT_CONFIG_*`, not `-c` on the
        // setup calls. Held under the flag guard so it doesn't race other tests.
        std::env::set_var("GIT_CONFIG_COUNT", "4");
        std::env::set_var("GIT_CONFIG_KEY_0", "protocol.file.allow");
        std::env::set_var("GIT_CONFIG_VALUE_0", "always");
        std::env::set_var("GIT_CONFIG_KEY_1", "user.email");
        std::env::set_var("GIT_CONFIG_VALUE_1", "t@e.com");
        std::env::set_var("GIT_CONFIG_KEY_2", "user.name");
        std::env::set_var("GIT_CONFIG_VALUE_2", "T");
        // Keep the test hermetic from any globally-installed hooks (a
        // `core.hooksPath` in the user's config would otherwise fire on commits).
        std::env::set_var("GIT_CONFIG_KEY_3", "core.hooksPath");
        std::env::set_var("GIT_CONFIG_VALUE_3", "/nonexistent-wits-test-hooks");
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        let run = |dir: &Path, args: &[&str]| {
            Command::new("git")
                .args(args.iter().copied())
                .current_dir(dir)
                .force_run()
                .exec()
                .unwrap();
        };
        // Two levels of nesting: P -> mid -> leaf, so the recursive borrow's
        // chaining (not just the top level) is exercised.
        let mk = |name: &str| {
            let d = root.join(name);
            run(root, &["init", "-q", "-b", "main", name]);
            std::fs::write(d.join("f"), "v1").unwrap();
            run(&d, &["add", "f"]);
            run(&d, &["commit", "-q", "-m", "c1"]);
            d
        };
        let leaf = mk("leaf");
        let mid = mk("mid");
        run(
            &mid,
            &[
                "submodule",
                "add",
                "-q",
                &format!("file://{}", leaf.display()),
                "leaf",
            ],
        );
        run(&mid, &["commit", "-q", "-m", "add leaf"]);
        let sup = root.join("P");
        run(root, &["init", "-q", "-b", "main", "P"]);
        run(
            &sup,
            &[
                "submodule",
                "add",
                "-q",
                &format!("file://{}", mid.display()),
                "mid",
            ],
        );
        run(&sup, &["commit", "-q", "-m", "add mid"]);
        // Primary initialises the whole tree, so every level's store exists to borrow.
        run(&sup, &["submodule", "update", "--init", "--recursive"]);

        // A linked worktree of the superproject — where a bare update would
        // re-clone every submodule from scratch.
        let wt = root.join("W");
        run(
            &sup,
            &[
                "worktree",
                "add",
                "-q",
                wt.to_str().unwrap(),
                "-b",
                "feat",
                "HEAD",
            ],
        );

        let common = Repository::new(&wt).git_common_dir().unwrap();
        let reference = common.join("modules").join("mid"); // the top submodule's store
        Repository::new(&wt)
            .submodule_init_borrow("mid", Some(&reference))
            .unwrap();

        // Assert borrowing at *both* levels: the top via the explicit reference,
        // the nested one via the alternateLocation chaining down to its own store.
        let alternates_of = |sub_rel: &str| {
            let gitdir = Repository::new(&wt.join(sub_rel)).git_dir().unwrap();
            std::fs::read_to_string(gitdir.join("objects/info/alternates")).unwrap_or_default()
        };
        let mid_alts = alternates_of("mid");
        assert!(
            mid_alts.contains(reference.to_str().unwrap()),
            "top submodule should borrow from {}, got: {mid_alts}",
            reference.display()
        );
        let leaf_store = common.join("modules/mid/modules/leaf");
        let leaf_alts = alternates_of("mid/leaf");
        assert!(
            leaf_alts.contains(leaf_store.to_str().unwrap()),
            "nested submodule should borrow from its own store {} (chained), got: {leaf_alts}",
            leaf_store.display()
        );
    }
}

/// Logically normalize a path, collapsing `.` and `..` without touching the
/// filesystem — so a relative `core.worktree` joined onto the git-dir resolves
/// the same way git resolves it, regardless of symlinks or missing intermediates.
fn normalize_path(path: std::path::PathBuf) -> std::path::PathBuf {
    use std::path::Component;
    let mut out = std::path::PathBuf::new();
    for comp in path.components() {
        match comp {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other),
        }
    }
    out
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
