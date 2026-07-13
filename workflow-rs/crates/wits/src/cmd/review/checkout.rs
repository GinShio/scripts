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

use wits_util::forge::MrState;
use wits_util::git::Repository;
use wits_util::project::git::Git;

use super::model::{short, state_word};
use super::store::refs;
use super::{local, CheckoutArgs, Local, PruneArgs};

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

pub fn run_prune(repo: &Repository, args: &PruneArgs) -> Result<()> {
    let ctx = local(repo)?;
    // The dormancy cutoff, as a Unix instant: an MR last fetched before it is
    // stale. `--older-than` is a day count or an ISO-8601 date.
    let cutoff = args.older_than.as_deref().map(parse_cutoff).transpose()?;

    let mut pruned = 0;
    for info in ctx.store.list_infos() {
        let id = &info.mr.id;
        let terminal = matches!(info.mr.state, MrState::Merged | MrState::Closed);
        let stale = cutoff.is_some_and(|before| {
            info.current()
                .and_then(|s| s.fetched_at.parse::<i64>().ok())
                .is_some_and(|at| at < before)
        });
        if !terminal && !stale {
            continue;
        }

        // Drop the snapshot pins so git can GC the objects, then the whole
        // per-MR directory.
        for (name, _) in ctx.repo.refs_under(&refs::mr_prefix(id)) {
            if let Err(e) = ctx.repo.delete_ref(&name) {
                log::warn!("MR {id}: could not delete {name}: {e}");
            }
        }
        ctx.store.delete_mr(id)?;
        let why = if terminal {
            state_word(info.mr.state, info.mr.draft)
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

/// Interpret `--older-than` as a number of days or an ISO-8601 date, returning
/// the Unix instant before which an MR counts as dormant.
fn parse_cutoff(spec: &str) -> Result<i64> {
    if let Ok(days) = spec.parse::<i64>() {
        return Ok(now_secs() - days.saturating_mul(86_400));
    }
    let epoch_day = iso_date_to_epoch_day(spec).with_context(|| {
        format!("--older-than must be a day count or an ISO date, got '{spec}'")
    })?;
    Ok(epoch_day * 86_400)
}

/// Days since the Unix epoch for a `YYYY-MM-DD` date (Hinnant's days_from_civil),
/// so an ISO date needs no date crate.
fn iso_date_to_epoch_day(date: &str) -> Option<i64> {
    let mut parts = date.split('-');
    let y: i64 = parts.next()?.trim().parse().ok()?;
    let m: i64 = parts.next()?.parse().ok()?;
    let d: i64 = parts.next()?.parse().ok()?;
    if parts.next().is_some() || !(1..=12).contains(&m) || d < 1 || d > days_in_month(y, m) {
        return None;
    }
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    Some(era * 146_097 + doe - 719_468)
}

/// Days in a Gregorian month, so an impossible date like `2026-02-31` is
/// rejected rather than silently over-counting.
fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
            if leap {
                29
            } else {
                28
            }
        }
        _ => 0,
    }
}

fn now_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso_date_epoch_and_validation() {
        // The epoch itself, and a known post-epoch day.
        assert_eq!(iso_date_to_epoch_day("1970-01-01"), Some(0));
        assert_eq!(iso_date_to_epoch_day("1970-01-02"), Some(1));
        // Impossible dates are rejected rather than silently over-counted.
        assert!(iso_date_to_epoch_day("2026-02-31").is_none());
        assert!(iso_date_to_epoch_day("2026-13-01").is_none());
        assert!(iso_date_to_epoch_day("2026-00-10").is_none());
        // Leap-day handling.
        assert!(iso_date_to_epoch_day("2024-02-29").is_some());
        assert!(iso_date_to_epoch_day("2026-02-29").is_none());
    }
}
