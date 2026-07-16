//! `wits review checkout` — put an MR's code somewhere runnable.
//!
//! Materialization is what lets a reviewer build, run, and fuzz an MR locally.
//! There is **one** review worktree (a sibling `../<main>.review`), reused for
//! every MR: `checkout <mr>` and `--next`/`--prev` just switch its HEAD to the
//! target snapshot. A stack therefore costs one worktree, not one per member,
//! and pruning a merged member never disturbs it. `--in-place` instead moves
//! HEAD in the current working tree (one at a time, hard-guarding a dirty tree
//! so reviewing someone else's work never buries yours).
//!
//! `--submodules` materialises the checkout's submodules. The **first** time it
//! borrows objects from your primary checkout (so even large submodules cost no
//! re-download); on a later HEAD switch it just **updates** the already-present
//! submodules to the new snapshot's pins — the borrow is a one-time
//! materialisation concern, not something to redo on every switch.

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

    // Materialise the checkout, idempotently — so a second run (e.g. to add
    // `--submodules`) reuses what the first made rather than erroring.
    let checkout = if args.in_place {
        // In-place operates on the *current* working tree — whichever worktree
        // the command was invoked from — since that is precisely what "check out
        // here" means.
        let current = ctx.repo.toplevel().unwrap_or_else(|| PathBuf::from("."));
        let git = Repository::new(&current);
        if git.is_dirty() {
            bail!(
                "working tree has uncommitted changes; commit or stash them first \
                 (in-place checkout moves HEAD and would bury your work)"
            );
        }
        git.checkout(&head)
            .with_context(|| format!("checking out MR {id} in place"))?;
        log::info!("checked out MR {id} at {} (detached HEAD)", short(&head));
        current
    } else {
        // One review worktree, re-pointed at each MR. The default location is
        // anchored to the *main* worktree, so it resolves to the same
        // `../<main>.review` no matter which worktree we run from.
        let dir = match &args.worktree {
            Some(dir) => dir.clone(),
            None => {
                let main = ctx
                    .repo
                    .main_worktree()
                    .or_else(|| ctx.repo.toplevel())
                    .unwrap_or_else(|| PathBuf::from("."));
                super::default_worktree_dir(&main)
            }
        };
        if dir.exists() {
            // Re-point the existing review worktree at this MR by switching HEAD.
            // Guard tracked changes (untracked build output is preserved by the
            // checkout); a stack switch that only moves submodule pins is not
            // "dirty" — `is_dirty` ignores submodules.
            let wt = Repository::new(&dir);
            if wt.is_dirty() {
                bail!(
                    "review worktree {} has uncommitted changes; commit or stash them \
                     before switching it to another MR",
                    dir.display()
                );
            }
            wt.checkout(&head)
                .with_context(|| format!("switching review worktree to MR {id}"))?;
            log::info!(
                "MR {id}: switched review worktree {} to {}",
                dir.display(),
                short(&head)
            );
        } else {
            if let Some(parent) = dir.parent() {
                std::fs::create_dir_all(parent)
                    .with_context(|| format!("creating {}", parent.display()))?;
            }
            // `git worktree add` targets the absolute `dir` regardless of which
            // worktree drives it, so the current checkout is a fine handle.
            ctx.repo
                .worktree_add(&dir, &head, false)
                .with_context(|| format!("adding worktree for MR {id}"))?;
            log::info!("MR {id} checked out into worktree {}", dir.display());
        }
        dir
    };

    if args.submodules {
        sync_submodules(&checkout, &id)?;
    }

    ctx.store.set_current(&id)?;
    Ok(())
}

/// Bring the checkout's submodules in line with this snapshot's pins.
///
/// Two paths, because with one reusable worktree most switches are between
/// snapshots of a stack that share the same submodules:
///
/// - a submodule **not yet materialised** (no `<sub>/.git`) is a *first*
///   materialisation — init it **borrowing objects** from the primary store
///   (`<git-common-dir>/modules/<path>`) so even a large submodule costs no
///   re-download; the nested levels chain the borrow via
///   [`Repository::submodule_init_borrow`];
/// - a submodule **already materialised** only needs its working tree moved to
///   the new pin — a plain `git submodule update`, no `--init`, no
///   `--reference`. The borrow was a one-time concern; re-passing it on every
///   HEAD switch would be pure waste.
fn sync_submodules(checkout: &Path, id: &str) -> Result<()> {
    let wt = Repository::new(checkout);
    let subs = wt.materialised_submodules();
    if subs.is_empty() {
        log::info!("MR {id}: no submodules present");
        return Ok(());
    }
    let modules = wt.git_common_dir().map(|c| c.join("modules"));
    for sub in &subs {
        if checkout.join(sub).join(".git").exists() {
            // Already materialised: just follow the pin (objects already local
            // or borrowed from the first pass).
            wt.submodule_update(std::slice::from_ref(sub), false)
                .with_context(|| format!("updating submodule '{sub}' for MR {id}"))?;
        } else {
            // First materialisation: borrow only from a *real* object store.
            // `modules/<name>` for a nested submodule path can be a bare
            // intermediate directory (the parent of the actual store); handing
            // that to `--reference` makes git abort with "not a repository", and
            // an `is_dir()` guard alone can't tell the two apart.
            let reference = modules
                .as_ref()
                .map(|m| m.join(sub))
                .filter(|r| is_object_store(r));
            wt.submodule_init_borrow(sub, reference.as_deref())
                .with_context(|| format!("initialising submodule '{sub}' for MR {id}"))?;
        }
    }
    log::info!("MR {id}: synced {} submodule(s)", subs.len());
    Ok(())
}

/// Whether `dir` is a git object store we can borrow from with `--reference` —
/// a submodule's git dir under `<common>/modules/<name>`, which carries an
/// `objects` directory. A plain intermediate directory (a nested submodule
/// path's parent) is not, and must never reach `--reference`.
fn is_object_store(dir: &Path) -> bool {
    dir.join("objects").is_dir()
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
