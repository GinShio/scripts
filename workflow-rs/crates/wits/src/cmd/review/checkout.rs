//! `wits review checkout` — put an MR's code somewhere runnable, and `prune` —
//! drop what's dead.
//!
//! Materialization is what lets a reviewer build, run, and fuzz an MR locally.
//! It supports both a worktree (the default — leaves your tree untouched, lets
//! you review several MRs at once) and an in-place checkout (moves HEAD, one at
//! a time, and hard-guards a dirty tree so reviewing someone else's work never
//! buries yours). `--next`/`--prev` walk the stack from the last checkout.

use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{bail, Context, Result};

use wits_util::git::Repository;
use wits_util::project::git::Git;

use super::store::refs;
use super::{local, CheckoutArgs, Local, PruneArgs};

pub fn run(repo: &Repository, args: &CheckoutArgs) -> Result<()> {
    let ctx = local(repo)?;
    let id = resolve_target(&ctx, args)?;

    let cache = ctx
        .store
        .load_cache(&id)
        .with_context(|| format!("MR {id} isn't fetched — run `wits review fetch {id}` first"))?;
    let head = cache.version.head_sha;
    if head.is_empty() {
        bail!("MR {id} has no fetched snapshot — run `wits review fetch {id}` for full detail");
    }

    let toplevel = ctx.repo.toplevel().unwrap_or_else(|| PathBuf::from("."));
    let git = Git::new(&toplevel);

    if args.in_place {
        if git.is_dirty() {
            bail!(
                "working tree has uncommitted changes; commit or stash them first \
                 (in-place checkout moves HEAD and would bury your work)"
            );
        }
        git.checkout(&head)
            .with_context(|| format!("checking out MR {id} in place"))?;
        log::info!("checked out MR {id} at {} (detached HEAD)", short(&head));
    } else {
        let dir = args
            .worktree
            .clone()
            .unwrap_or_else(|| default_worktree_dir(&toplevel, &ctx.target.repo, &id));
        if let Some(parent) = dir.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("creating {}", parent.display()))?;
        }
        git.worktree_add(&dir, &head, false)
            .with_context(|| format!("adding worktree for MR {id}"))?;
        log::info!("MR {id} checked out into worktree {}", dir.display());
    }

    ctx.store.set_current(&id)?;
    Ok(())
}

/// The MR to materialize: explicit, or the neighbour of the current checkout.
fn resolve_target(ctx: &Local, args: &CheckoutArgs) -> Result<String> {
    if let Some(handle) = &args.mr {
        return super::parse_mr_handle(handle);
    }
    if !args.next && !args.prev {
        bail!("give an MR to check out, or use --next/--prev");
    }

    let current = ctx
        .store
        .current()
        .context("no current review to navigate from; check out an MR first")?;
    let caches = ctx.store.list_cached();
    let (chain, pos) = super::stack_chain(&caches, &current);
    let target = if args.next {
        chain.get(pos + 1)
    } else {
        pos.checked_sub(1).and_then(|i| chain.get(i))
    };
    target.cloned().with_context(|| {
        let edge = if args.next { "top" } else { "bottom" };
        format!("already at the {edge} of the stack (current MR {current})")
    })
}

/// A sibling directory, so review worktrees don't clutter the checkout: e.g.
/// `../<repo>.review/mr-123`.
fn default_worktree_dir(toplevel: &std::path::Path, repo: &str, id: &str) -> PathBuf {
    let parent = toplevel.parent().unwrap_or(toplevel);
    parent
        .join(format!("{repo}.review"))
        .join(format!("mr-{id}"))
}

pub fn run_prune(repo: &Repository, args: &PruneArgs) -> Result<()> {
    let ctx = local(repo)?;
    let now = now_secs();
    let cutoff = args.older_than.map(|days| days * 86_400);

    let mut pruned = 0;
    for cache in ctx.store.list_cached() {
        let id = &cache.mr.id;
        let terminal = matches!(cache.mr.state.as_str(), "merged" | "closed");
        let stale = cutoff.is_some_and(|window| {
            cache
                .fetched_at
                .parse::<u64>()
                .ok()
                .is_some_and(|at| now.saturating_sub(at) > window)
        });
        if !terminal && !stale {
            continue;
        }

        // Drop the snapshot pins so git can GC the objects, then the cache and
        // any (now moot) draft.
        for (name, _) in ctx.repo.refs_under(&refs::mr_prefix(id)) {
            if let Err(e) = ctx.repo.delete_ref(&name) {
                log::warn!("MR {id}: could not delete {name}: {e}");
            }
        }
        ctx.store.delete_cache(id)?;
        ctx.store.delete_draft(id)?;
        let why = if terminal {
            cache.mr.state.as_str()
        } else {
            "dormant"
        };
        log::info!("pruned MR {id} ({why})");
        pruned += 1;
    }

    if pruned == 0 {
        log::info!("nothing to prune");
    } else {
        log::info!("pruned {pruned} MR(s)");
    }
    Ok(())
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn short(sha: &str) -> &str {
    &sha[..sha.len().min(8)]
}
