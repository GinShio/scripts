//! The local store: three JSON files per MR, and how they are addressed.
//!
//! Each MR gets a directory holding `info.json` (metadata + diff state),
//! `comments.json` (the forge's discussion, a cache), and `local.json` (your
//! unsubmitted actions — the one file you edit). The root is resolved on the
//! `WITS_REVIEW_DIR` → `$XDG_STATE_HOME/wits/review` → `$GIT_DIR/wits/review`
//! ladder, then keyed by the repo's `host/owner/repo` so one central root can
//! hold many repos and a store migrates cleanly between roots.

use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

use wits_util::git::Repository;
use wits_util::remote::RemoteInfo;

use super::model::{Comments, Info, Local};

/// The per-repo root under which one repo's review state lives.
pub struct Store {
    root: PathBuf,
}

fn base_dir(repo: &Repository) -> Result<PathBuf> {
    if let Some(dir) = std::env::var_os("WITS_REVIEW_DIR") {
        return Ok(PathBuf::from(dir));
    }
    if let Some(state) = std::env::var_os("XDG_STATE_HOME") {
        return Ok(PathBuf::from(state).join("wits").join("review"));
    }
    let git_dir = repo
        .git_dir()
        .context("not inside a git repository (no .git dir)")?;
    Ok(git_dir.join("wits").join("review"))
}

impl Store {
    pub fn open(repo: &Repository, target: &RemoteInfo) -> Result<Store> {
        let root = base_dir(repo)?
            .join(&target.host)
            .join(&target.owner)
            .join(&target.repo);
        Ok(Store { root })
    }

    fn mr_dir(&self, id: &str) -> PathBuf {
        self.root.join(id)
    }

    // -- info (metadata + diff state) ----------------------------------------

    pub fn load_info(&self, id: &str) -> Option<Info> {
        read_json(&self.mr_dir(id).join("info.json"))
    }

    pub fn save_info(&self, id: &str, info: &Info) -> Result<()> {
        write_json(&self.mr_dir(id).join("info.json"), info)
    }

    // -- comments (remote discussion cache) ----------------------------------

    pub fn load_comments(&self, id: &str) -> Comments {
        read_json(&self.mr_dir(id).join("comments.json")).unwrap_or_default()
    }

    pub fn save_comments(&self, id: &str, comments: &Comments) -> Result<()> {
        write_json(&self.mr_dir(id).join("comments.json"), comments)
    }

    // -- local (the editable draft) ------------------------------------------

    pub fn load_local(&self, id: &str) -> Local {
        read_json(&self.mr_dir(id).join("local.json")).unwrap_or_default()
    }

    /// Persist the draft, or delete the file once it has emptied — an empty
    /// draft and no draft are the same thing.
    pub fn save_local(&self, id: &str, local: &Local) -> Result<()> {
        let path = self.mr_dir(id).join("local.json");
        if local.is_empty() {
            return remove_if_present(&path);
        }
        write_json(&path, local)
    }

    // -- enumeration & removal -----------------------------------------------

    /// Every MR's `info`, for the inbox and stack reconstruction.
    pub fn list_infos(&self) -> Vec<Info> {
        self.mr_ids()
            .iter()
            .filter_map(|id| self.load_info(id))
            .collect()
    }

    /// The ids of MRs that have a non-empty local draft.
    pub fn local_ids(&self) -> Vec<String> {
        self.mr_ids()
            .into_iter()
            .filter(|id| self.mr_dir(id).join("local.json").exists())
            .collect()
    }

    /// Drop an MR's whole directory — for `prune`.
    pub fn delete_mr(&self, id: &str) -> Result<()> {
        let dir = self.mr_dir(id);
        if dir.exists() {
            fs::remove_dir_all(&dir).with_context(|| format!("removing {}", dir.display()))?;
        }
        Ok(())
    }

    /// The immediate subdirectory names of the repo root — one per MR.
    fn mr_ids(&self) -> Vec<String> {
        let Ok(entries) = fs::read_dir(&self.root) else {
            return Vec::new();
        };
        let mut ids: Vec<String> = entries
            .flatten()
            .filter(|e| e.path().is_dir())
            .filter_map(|e| e.file_name().to_str().map(str::to_owned))
            .collect();
        ids.sort();
        ids
    }

    // -- current-checkout pointer --------------------------------------------

    pub fn current(&self) -> Option<String> {
        fs::read_to_string(self.root.join("current"))
            .ok()
            .map(|s| s.trim().to_owned())
            .filter(|s| !s.is_empty())
    }

    pub fn set_current(&self, id: &str) -> Result<()> {
        fs::create_dir_all(&self.root)
            .with_context(|| format!("creating {}", self.root.display()))?;
        fs::write(self.root.join("current"), id).context("recording current review")?;
        Ok(())
    }
}

/// The git-ref namespace that pins a reviewed snapshot's objects alive.
pub mod refs {
    pub fn pin(mr: &str, sha: &str) -> String {
        format!("refs/wits/review/{mr}/{sha}")
    }

    pub fn base_pin(mr: &str, sha: &str) -> String {
        format!("refs/wits/review/{mr}/{sha}-base")
    }

    pub fn mr_prefix(mr: &str) -> String {
        format!("refs/wits/review/{mr}/")
    }
}

fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> Option<T> {
    let text = fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

fn write_json<T: serde::Serialize>(path: &Path, value: &T) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).with_context(|| format!("creating {}", parent.display()))?;
    }
    let text = serde_json::to_string_pretty(value)?;
    fs::write(path, text).with_context(|| format!("writing {}", path.display()))?;
    Ok(())
}

fn remove_if_present(path: &Path) -> Result<()> {
    if path.exists() {
        fs::remove_file(path).with_context(|| format!("removing {}", path.display()))?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cmd::review::model::{Action, Local, SCHEMA};

    fn store() -> (tempfile::TempDir, Store) {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("store");
        (dir, Store { root })
    }

    #[test]
    fn info_round_trips_and_lists() {
        let (_g, store) = store();
        let info = crate::cmd::review::stub_info("7", "main", "feat");
        assert!(store.load_info("7").is_none());
        store.save_info("7", &info).unwrap();
        assert_eq!(store.load_info("7").unwrap().mr.id, "7");
        assert_eq!(store.list_infos().len(), 1);

        store.delete_mr("7").unwrap();
        assert!(store.load_info("7").is_none());
        assert!(store.list_infos().is_empty());
    }

    #[test]
    fn local_persists_and_vanishes_when_empty() {
        let (_g, store) = store();
        let mut local = Local::default();
        local.actions.push(Action::Comment {
            file: Some("a.c".into()),
            line: Some(3),
            side: None,
            start_line: None,
            body: "hi".into(),
            commit: None,
        });
        store.save_local("7", &local).unwrap();
        assert_eq!(store.load_local("7").actions.len(), 1);
        assert_eq!(store.local_ids(), ["7"]);

        store
            .save_local(
                "7",
                &Local {
                    schema: SCHEMA,
                    ..Default::default()
                },
            )
            .unwrap();
        assert!(store.local_ids().is_empty());
    }

    #[test]
    fn current_pointer_round_trips() {
        let (_g, store) = store();
        assert!(store.current().is_none());
        store.set_current("42").unwrap();
        assert_eq!(store.current().as_deref(), Some("42"));
    }
}
