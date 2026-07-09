//! The local store: where review state lives on disk, and how it is addressed.
//!
//! Two documents per MR, with opposite lifetimes (see `model`): a refetchable
//! `remote/mr-<id>.json` cache and a precious `draft/mr-<id>.json`. The root is
//! resolved on the `WITS_REVIEW_DIR` → `$XDG_STATE_HOME/wits/review` →
//! `$GIT_DIR/wits/review` ladder, then keyed by the repo's `host/owner/repo` so
//! one central root can hold many repos and a store migrates cleanly between
//! roots.
//!
//! The store's on-disk shape is private: editors read through `--json`, never
//! these files, so this layout is free to change.

use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

use wits_util::git::Repository;
use wits_util::remote::RemoteInfo;

use super::model::{Draft, RemoteCache};

/// The per-repo root under which one repo's review state lives.
pub struct Store {
    root: PathBuf,
}

/// Resolve the base directory that holds every repo's review state.
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
    /// Open (without creating) the store for one repo, keyed by its target
    /// remote's identity.
    pub fn open(repo: &Repository, target: &RemoteInfo) -> Result<Store> {
        let root = base_dir(repo)?
            .join(&target.host)
            .join(&target.owner)
            .join(&target.repo);
        Ok(Store { root })
    }

    fn remote_path(&self, id: &str) -> PathBuf {
        self.root.join("remote").join(format!("mr-{id}.json"))
    }

    fn draft_path(&self, id: &str) -> PathBuf {
        self.root.join("draft").join(format!("mr-{id}.json"))
    }

    /// Read the cached forge state for one MR, if present.
    pub fn load_cache(&self, id: &str) -> Option<RemoteCache> {
        read_json(&self.remote_path(id))
    }

    /// Overwrite the cached forge state for one MR (it is disposable).
    pub fn save_cache(&self, id: &str, cache: &RemoteCache) -> Result<()> {
        write_json(&self.remote_path(id), cache)
    }

    /// The draft for one MR, or a fresh empty one when none exists.
    pub fn load_draft(&self, id: &str) -> Draft {
        read_json(&self.draft_path(id)).unwrap_or_default()
    }

    /// Persist a draft, or delete its file once it has emptied — an empty draft
    /// is indistinguishable from no draft, and keeping a stale empty file around
    /// would only be noise.
    pub fn save_draft(&self, id: &str, draft: &Draft) -> Result<()> {
        if draft.is_empty() {
            return self.delete_draft(id);
        }
        write_json(&self.draft_path(id), draft)
    }

    /// Remove a draft file (no-op when absent).
    pub fn delete_draft(&self, id: &str) -> Result<()> {
        let path = self.draft_path(id);
        if path.exists() {
            fs::remove_file(&path).with_context(|| format!("removing draft {}", path.display()))?;
        }
        Ok(())
    }

    /// Remove an MR's cache file (no-op when absent) — for `prune`.
    pub fn delete_cache(&self, id: &str) -> Result<()> {
        let path = self.remote_path(id);
        if path.exists() {
            fs::remove_file(&path).with_context(|| format!("removing cache {}", path.display()))?;
        }
        Ok(())
    }

    /// Every cached MR, for the inbox and for stack reconstruction.
    pub fn list_cached(&self) -> Vec<RemoteCache> {
        list_json(&self.root.join("remote"))
    }

    /// The MR most recently `checkout`-ed, the origin `checkout --next/--prev`
    /// navigate from. Stored as one small file per repo.
    pub fn current(&self) -> Option<String> {
        fs::read_to_string(self.root.join("current"))
            .ok()
            .map(|s| s.trim().to_owned())
            .filter(|s| !s.is_empty())
    }

    pub fn set_current(&self, id: &str) -> Result<()> {
        fs::create_dir_all(&self.root)
            .with_context(|| format!("creating {}", self.root.display()))?;
        fs::write(self.root.join("current"), id)
            .with_context(|| "recording current review".to_string())?;
        Ok(())
    }

    /// The ids of every MR with a pending draft.
    pub fn draft_ids(&self) -> Vec<String> {
        let dir = self.root.join("draft");
        let Ok(entries) = fs::read_dir(&dir) else {
            return Vec::new();
        };
        entries
            .flatten()
            .filter_map(|e| {
                let name = e.file_name();
                let name = name.to_str()?;
                name.strip_prefix("mr-")?
                    .strip_suffix(".json")
                    .map(str::to_owned)
            })
            .collect()
    }
}

/// The git-ref namespace that pins a reviewed snapshot's objects alive. Names
/// carry only what disambiguates within a clone: the MR number and the SHA.
pub mod refs {
    /// The pin for one MR at one snapshot SHA.
    pub fn pin(mr: &str, sha: &str) -> String {
        format!("refs/wits/review/{mr}/{sha}")
    }

    /// The pin for that snapshot's base object, when it isn't an ancestor of the
    /// head (so it wouldn't otherwise stay reachable).
    pub fn base_pin(mr: &str, sha: &str) -> String {
        format!("refs/wits/review/{mr}/{sha}-base")
    }

    /// The prefix under which all of one MR's pins live.
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

fn list_json<T: serde::de::DeserializeOwned>(dir: &Path) -> Vec<T> {
    let Ok(entries) = fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut paths: Vec<PathBuf> = entries
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("json"))
        .collect();
    paths.sort();
    paths.iter().filter_map(|p| read_json(p)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cmd::review::model::{Action, Draft, Placement};
    use wits_util::forge::Side;

    fn store() -> (tempfile::TempDir, Store) {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("store");
        (dir, Store { root })
    }

    #[test]
    fn cache_round_trips() {
        let (_g, store) = store();
        let cache = crate::cmd::review::stub_cache("7", "main", "feat");
        assert!(store.load_cache("7").is_none());
        store.save_cache("7", &cache).unwrap();
        assert_eq!(store.load_cache("7").unwrap().mr.id, "7");
        assert_eq!(store.list_cached().len(), 1);

        store.delete_cache("7").unwrap();
        assert!(store.load_cache("7").is_none());
    }

    #[test]
    fn draft_persists_and_vanishes_when_empty() {
        let (_g, store) = store();
        let mut draft = Draft::default();
        let id = draft.next_id();
        draft.actions.push(Action::Comment {
            id: id.clone(),
            placement: Placement::Line {
                path: "a.c".into(),
                old_path: None,
                side: Side::New,
                line: 3,
                start_line: None,
                commit: Some("deadbeef".into()),
            },
            body: "hi".into(),
        });
        store.save_draft("7", &draft).unwrap();

        let loaded = store.load_draft("7");
        assert_eq!(loaded.actions.len(), 1);
        assert_eq!(store.draft_ids(), ["7"]);

        // Emptying the draft removes its file.
        let mut empty = loaded;
        empty.remove(&id);
        store.save_draft("7", &empty).unwrap();
        assert!(store.draft_ids().is_empty());
    }

    #[test]
    fn current_pointer_round_trips() {
        let (_g, store) = store();
        assert!(store.current().is_none());
        store.set_current("42").unwrap();
        assert_eq!(store.current().as_deref(), Some("42"));
    }
}
