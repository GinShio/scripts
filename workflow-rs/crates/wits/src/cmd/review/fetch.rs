//! `wits review fetch` — bring the local store in line with the forge.
//!
//! Idempotent, like `git fetch`: the first pull and a refresh are the same verb.
//! Fetching one MR pulls its metadata, its objects (pinned by a
//! `refs/wits/review/*` ref so a later force-push can't GC them), and its review
//! threads. Fetching a feed is lighter — it refreshes only the inbox summaries,
//! leaving the expensive per-MR pull to an explicit `fetch <mr>`.

use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};

use wits_util::git::Repository;

use super::model::{MrInfo, RemoteCache, StoredCommit, StoredFile, Thread, SCHEMA};
use super::store::refs;
use super::{online, parse_mr_handle, Online};

pub fn run(repo: &Repository, args: &super::FetchArgs) -> Result<()> {
    let ctx = online(repo)?;
    let remote = target_remote(&ctx);

    if let Some(handle) = &args.mr {
        let id = parse_mr_handle(handle)?;
        fetch_one(&ctx, &remote, &id)?;
        log::info!("fetched {} {}", ctx.forge.noun(), id);
        return Ok(());
    }

    if let Some(feed) = &args.feed {
        return fetch_feed(&ctx, feed);
    }

    if args.all {
        let ids: Vec<String> = ctx
            .local
            .store
            .list_cached()
            .into_iter()
            .map(|c| c.mr.id)
            .collect();
        if ids.is_empty() {
            log::info!("nothing in the store to refresh");
            return Ok(());
        }
        let mut failures = 0;
        for id in &ids {
            if let Err(e) = fetch_one(&ctx, &remote, id) {
                failures += 1;
                log::warn!("{id}: {e}");
            }
        }
        log::info!("refreshed {} MR(s)", ids.len() - failures);
        if failures > 0 {
            anyhow::bail!("{failures} MR(s) failed to refresh");
        }
        return Ok(());
    }

    anyhow::bail!("give an MR number, --feed <name>, or --all");
}

/// Re-fetch one MR after a submit, so freshly-posted threads come back. A thin
/// wrapper other verbs can call without knowing the remote name.
pub(crate) fn refresh(ctx: &Online, id: &str) -> Result<()> {
    let remote = target_remote(ctx);
    fetch_one(ctx, &remote, id)
}

/// The git remote name to fetch MR refs from — the merge target.
fn target_remote(ctx: &Online) -> String {
    if ctx.remotes.upstream.is_some() {
        "upstream".to_owned()
    } else {
        "origin".to_owned()
    }
}

/// Fully fetch one MR: metadata, objects (pinned), and threads.
fn fetch_one(ctx: &Online, remote: &str, id: &str) -> Result<()> {
    let forge = ctx.forge.as_ref();
    let store = &ctx.local.store;
    let repo = &ctx.local.repo;

    let details = forge.mr_details(id)?;
    let version = details.version;
    let head_sha = version.head_sha.clone();

    // Pull and pin the head, then best-effort pull the base (it may not be an
    // ancestor of the head, so it wouldn't otherwise be reachable).
    if !head_sha.is_empty() {
        let mr_ref = forge.mr_ref(id)?;
        let pin = refs::pin(id, &head_sha);
        repo.fetch_ref(remote, &mr_ref, &pin)
            .with_context(|| format!("fetching MR {id} objects"))?;
    }
    if !version.base_sha.is_empty() && repo.rev_parse(&version.base_sha).is_none() {
        repo.try_fetch_object(remote, &version.base_sha, &refs::base_pin(id, &head_sha));
    }

    // Commits and files are derived locally from the fetched objects, not the
    // forge — the diff coordinates are ours to compute.
    let (commits, files) = local_range(repo, &version.base_sha, &head_sha);
    let threads: Vec<Thread> = forge
        .list_threads(id)?
        .into_iter()
        .map(Thread::from)
        .collect();

    let cache = RemoteCache {
        schema: SCHEMA,
        mr: MrInfo::from(details.summary),
        version,
        fetched_at: now_epoch(),
        commits,
        files,
        threads,
    };
    store.save_cache(id, &cache)?;
    Ok(())
}

/// Refresh a feed's inbox summaries — cheap, no objects or threads. Where a full
/// cache already exists, only its metadata is refreshed; its threads and derived
/// diff are left intact so a light feed refresh never discards a full pull.
fn fetch_feed(ctx: &Online, feed_name: &str) -> Result<()> {
    let cfg = super::config::Config::load()?;
    let key = super::config::repo_key(&ctx.local.target);
    let query = cfg.feed(&key, feed_name, None).with_context(|| {
        let known = cfg.feed_names(&key);
        if known.is_empty() {
            format!("no feed '{feed_name}' — no feeds configured for {key}")
        } else {
            format!(
                "no feed '{feed_name}'; configured feeds: {}",
                known.join(", ")
            )
        }
    })?;

    let summaries = ctx.forge.list_mrs(&query)?;
    let store = &ctx.local.store;
    for summary in &summaries {
        let info = MrInfo::from(summary.clone());
        let cache = match store.load_cache(&info.id) {
            Some(mut existing) => {
                existing.mr = info;
                existing
            }
            None => RemoteCache {
                schema: SCHEMA,
                mr: info,
                version: Default::default(),
                fetched_at: now_epoch(),
                commits: Vec::new(),
                files: Vec::new(),
                threads: Vec::new(),
            },
        };
        store.save_cache(&cache.mr.id, &cache)?;
    }
    log::info!(
        "feed '{feed_name}': {} MR(s) (run `wits review fetch <mr>` for full detail)",
        summaries.len()
    );
    Ok(())
}

/// Commits (oldest-first) and changed files in `base..head`, derived from the
/// fetched objects. Empty when the range can't be computed locally.
fn local_range(repo: &Repository, base: &str, head: &str) -> (Vec<StoredCommit>, Vec<StoredFile>) {
    if base.is_empty() || head.is_empty() {
        return (Vec::new(), Vec::new());
    }
    let range = format!("{base}..{head}");
    let commits = repo
        .commits(&range)
        .into_iter()
        .map(|c| StoredCommit {
            sha: c.hash,
            subject: c.subject,
        })
        .collect();
    let files = repo
        .changed_files(&range)
        .into_iter()
        .map(|f| StoredFile {
            path: f.path,
            old_path: f.old_path,
            status: f.status.to_string(),
        })
        .collect();
    (commits, files)
}

fn now_epoch() -> String {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs().to_string())
        .unwrap_or_default()
}
