//! `wits review fetch` — bring the local store in line with the forge.
//!
//! Idempotent, like `git fetch`. Fetching one MR is a *full* pull: metadata and
//! diff state into `info.json`, the discussion into `comments.json`, and the
//! objects pinned by a `refs/wits/review/*` ref so a later force-push can't GC
//! them. Fetching a feed is *light* — it refreshes only `info.json` for each
//! matching MR, leaving the expensive per-MR pull to `fetch <mr>`. With no
//! argument, every configured feed is refreshed (the RSS "refresh all").

use anyhow::{Context, Result};

use wits_util::forge::DiffVersion;
use wits_util::git::Repository;

use super::config::{self, Config};
use super::model::{range_artifacts, Comments, Info, StoredCommit, StoredFile, Thread, SCHEMA};
use super::store::refs;
use super::{now_secs, online, parse_mr_handle, Online};

pub fn run(repo: &Repository, args: &super::FetchArgs) -> Result<()> {
    let ctx = online(repo)?;

    if let Some(handle) = &args.mr {
        let id = parse_mr_handle(handle)?;
        fetch_one(&ctx, &target_remote(&ctx), &id)?;
        log::info!("fetched {} {}", ctx.forge.noun(), id);
        return Ok(());
    }

    let cfg = Config::load()?;
    let key = config::repo_key(&ctx.local.target);
    match &args.feed {
        Some(name) => fetch_feed(&ctx, &cfg, &key, name),
        None => fetch_all_feeds(&ctx, &cfg, &key),
    }
}

/// The git remote name to fetch MR refs from — the merge target.
fn target_remote(ctx: &Online) -> String {
    if ctx.remotes.upstream.is_some() {
        "upstream".to_owned()
    } else {
        "origin".to_owned()
    }
}

/// Re-fetch one MR after a submit, so freshly-posted threads come back.
pub(crate) fn refresh(ctx: &Online, id: &str) -> Result<()> {
    fetch_one(ctx, &target_remote(ctx), id)
}

/// Fully fetch one MR: metadata + diff state, discussion, and pinned objects.
fn fetch_one(ctx: &Online, remote: &str, id: &str) -> Result<()> {
    let forge = ctx.forge.as_ref();
    let store = &ctx.local.store;
    let repo = &ctx.local.repo;

    let details = forge.mr_details(id)?;
    let v = details.version;
    let head_sha = v.head_sha.clone();

    if !head_sha.is_empty() {
        let mr_ref = forge.mr_ref(id)?;
        repo.fetch_ref(remote, &mr_ref, &refs::pin(id, &head_sha))
            .with_context(|| format!("fetching MR {id} objects"))?;
    }
    if !v.base_sha.is_empty() && repo.rev_parse(&v.base_sha).is_none() {
        repo.try_fetch_object(remote, &v.base_sha, &refs::base_pin(id, &head_sha));
    }

    let (commits, files) = local_range(repo, &v.base_sha, &head_sha);
    // Preserve the snapshot history across fetches; append only when the head
    // moved. Metadata and current-snapshot diff state are refreshed wholesale,
    // and `fetched_at` is stamped every time (even for an unchanged head) so
    // dormancy tracks real sync time.
    let mut info = Info {
        schema: SCHEMA,
        mr: details.summary,
        snapshots: store.load_info(id).map(|i| i.snapshots).unwrap_or_default(),
        fetched_at: now_secs(),
        commits,
        files,
    };
    info.record_snapshot(DiffVersion {
        base_sha: v.base_sha,
        start_sha: v.start_sha,
        head_sha,
    });
    store.save_info(id, &info)?;

    let threads: Vec<Thread> = forge
        .list_threads(id)?
        .into_iter()
        .map(Thread::from)
        .collect();
    store.save_comments(
        id,
        &Comments {
            schema: SCHEMA,
            threads,
        },
    )?;
    Ok(())
}

/// Refresh one feed's inbox summaries — cheap, `info.json` only. Where a full
/// pull already exists, only the summary is refreshed; its diff state and
/// discussion are left intact.
fn fetch_feed(ctx: &Online, cfg: &Config, key: &str, name: &str) -> Result<()> {
    let query = cfg.feed(key, name, None).with_context(|| {
        let known = cfg.feed_names(key);
        if known.is_empty() {
            format!("no feed '{name}' — no feeds configured for {key}")
        } else {
            format!("no feed '{name}'; configured feeds: {}", known.join(", "))
        }
    })?;

    let summaries = ctx.forge.list_mrs(&query)?;
    let store = &ctx.local.store;
    for summary in summaries.iter() {
        let mr = summary.clone();
        let mut info = match store.load_info(&mr.id) {
            Some(mut existing) => {
                existing.mr = mr;
                existing
            }
            None => Info {
                schema: SCHEMA,
                mr,
                snapshots: Vec::new(),
                fetched_at: 0,
                commits: Vec::new(),
                files: Vec::new(),
            },
        };
        info.fetched_at = now_secs();
        store.save_info(&info.mr.id, &info)?;
    }
    log::info!(
        "feed '{name}': {} MR(s) (run `wits review fetch <mr>` for full detail)",
        summaries.len()
    );
    Ok(())
}

/// Refresh every configured feed for the repo.
fn fetch_all_feeds(ctx: &Online, cfg: &Config, key: &str) -> Result<()> {
    let names = cfg.feed_names(key);
    if names.is_empty() {
        anyhow::bail!(
            "no feeds configured for {key}; give an MR number, or add a feed to review.toml"
        );
    }
    let mut failures = 0;
    for name in &names {
        if let Err(e) = fetch_feed(ctx, cfg, key, name) {
            failures += 1;
            log::warn!("feed '{name}': {e}");
        }
    }
    if failures > 0 {
        anyhow::bail!("{failures} feed(s) failed to refresh");
    }
    Ok(())
}

/// Commits (oldest-first) and changed files in `base..head`, derived from the
/// fetched objects. Empty when the range can't be computed locally (a missing
/// endpoint), delegating the mapping to [`range_artifacts`].
fn local_range(repo: &Repository, base: &str, head: &str) -> (Vec<StoredCommit>, Vec<StoredFile>) {
    if base.is_empty() || head.is_empty() {
        return (Vec::new(), Vec::new());
    }
    range_artifacts(repo, &format!("{base}..{head}"))
}
