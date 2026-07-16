//! `wits review checkout` — put an MR's code somewhere runnable.
//!
//! Materialization is what lets a reviewer build, run, and fuzz an MR locally.
//! It supports both a worktree (the default — leaves your tree untouched, lets
//! you review several MRs at once) and an in-place checkout (moves HEAD, one at
//! a time, and hard-guards a dirty tree so reviewing someone else's work never
//! buries yours). `--next`/`--prev` walk the stack from the last checkout.
//!
//! The checkout is **reusable**: making it is idempotent (an existing worktree
//! is reused, not re-created), so a first, lightweight pass — just the code —
//! can be followed by `--submodules` to materialise submodules on the same
//! checkout when you actually need to build. Submodule init borrows objects
//! from your primary checkout and fetches shallow, so it stays cheap even for
//! the large submodules of a monorepo.

use std::path::{Path, PathBuf};

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

    // Materialise the checkout, idempotently — so a second run (e.g. to add
    // `--submodules`) reuses what the first made rather than erroring.
    let checkout = if args.in_place {
        if git.is_dirty() {
            bail!(
                "working tree has uncommitted changes; commit or stash them first \
                 (in-place checkout moves HEAD and would bury your work)"
            );
        }
        git.checkout(&head)
            .with_context(|| format!("checking out MR {id} in place"))?;
        log::info!("checked out MR {id} at {} (detached HEAD)", short(&head));
        toplevel.clone()
    } else {
        let dir = args
            .worktree
            .clone()
            .unwrap_or_else(|| super::default_worktree_dir(&toplevel, &ctx.target.repo, &id));
        if dir.exists() {
            log::info!("MR {id}: reusing worktree {}", dir.display());
        } else {
            if let Some(parent) = dir.parent() {
                std::fs::create_dir_all(parent)
                    .with_context(|| format!("creating {}", parent.display()))?;
            }
            git.worktree_add(&dir, &head, false)
                .with_context(|| format!("adding worktree for MR {id}"))?;
            log::info!("MR {id} checked out into worktree {}", dir.display());
        }
        dir
    };

    if args.submodules {
        init_submodules(&checkout, &id)?;
    }

    ctx.store.set_current(&id)?;
    Ok(())
}

/// Materialise the checkout's submodules for this snapshot, **borrowing
/// objects** from the primary checkout so nothing is re-downloaded — the whole
/// nested tree, at every level.
///
/// A linked worktree does not share the primary's submodule object store (it
/// would re-clone in full). We iterate this repo's **direct** submodules — only
/// those **present** in the (possibly sparse) checkout — and hand each its
/// primary store, `<git-common-dir>/modules/<path>`, as a `--reference`. The
/// loop is per direct submodule because `--reference` is a single real
/// repository; the levels *beneath* each are then borrowed by the chaining
/// inside [`Repository::submodule_init_borrow`] (`alternateLocation=superproject`),
/// so a deep tree costs no re-download anywhere.
fn init_submodules(checkout: &Path, id: &str) -> Result<()> {
    let wt = Repository::new(checkout);
    let subs = wt.materialised_submodules();
    if subs.is_empty() {
        log::info!("MR {id}: no submodules present to initialise");
        return Ok(());
    }
    // The primary's object store for a submodule at `<p>` is
    // `<git-common-dir>/modules/<p>` (the usual name==path layout). When it is
    // absent (the primary never initialised it, or its name differs from its
    // path) we simply don't borrow and let the shallow fetch stand — correct,
    // just not free.
    let modules = wt.git_common_dir().map(|c| c.join("modules"));
    for sub in &subs {
        let reference = modules.as_ref().map(|m| m.join(sub)).filter(|r| r.is_dir());
        wt.submodule_init_borrow(sub, reference.as_deref())
            .with_context(|| format!("initialising submodule '{sub}' for MR {id}"))?;
    }
    log::info!("MR {id}: initialised {} submodule(s)", subs.len());
    Ok(())
}

/// The MR to materialise: explicit, or the neighbour of the current checkout.
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
