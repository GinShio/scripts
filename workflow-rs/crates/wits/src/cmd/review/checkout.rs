//! `wits review checkout` — put an MR's code somewhere runnable.
//!
//! Materialization is what lets a reviewer build, run, and fuzz an MR locally.
//! It supports both a worktree (the default — leaves your tree untouched, lets
//! you review several MRs at once) and an in-place checkout (moves HEAD, one at
//! a time, and hard-guards a dirty tree so reviewing someone else's work never
//! buries yours). `--next`/`--prev` walk the stack from the last checkout.

use std::path::PathBuf;

use anyhow::{bail, Context, Result};

use wits_util::git::Repository;

use super::model::short;
use super::{local, CheckoutArgs, ReviewCtx};

pub fn run(repo: &Repository, args: &CheckoutArgs) -> Result<()> {
    let ctx = local(repo)?;
    let id = resolve_target(&ctx, args)?;

    let info = ctx
        .store
        .load_info(&id)
        .with_context(|| format!("MR {id} isn't fetched — run `wits review fetch {id}` first"))?;
    let Some(head) = info.head().map(str::to_owned) else {
        bail!("MR {id} has no fetched snapshot — run `wits review fetch {id}` for full detail");
    };

    let toplevel = ctx.repo.toplevel().unwrap_or_else(|| PathBuf::from("."));
    let git = Repository::new(&toplevel);

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
fn resolve_target(ctx: &ReviewCtx, args: &CheckoutArgs) -> Result<String> {
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
    let infos = ctx.store.list_infos();
    let (chain, pos) = super::stack_chain(&infos, &current);
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
