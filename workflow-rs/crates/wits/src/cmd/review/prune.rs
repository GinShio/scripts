//! `wits review prune` — drop the store for MRs that are done or gone quiet.
//!
//! Terminal MRs (merged/closed) are always dropped; `--older-than` also catches
//! *dormant* ones — those not fetched within a day count or since an ISO date.
//! Pruning removes both the per-MR directory and the snapshot pins
//! (`refs/wits/review/*`), so git can finally collect the reviewed objects.

use anyhow::{bail, Context, Result};

use wits_util::forge::MrState;
use wits_util::git::Repository;

use super::model::state_word;
use super::store::refs;
use super::{default_worktree_dir, local, parse_mr_handle, PruneArgs, ReviewCtx};

pub fn run(repo: &Repository, args: &PruneArgs) -> Result<()> {
    let ctx = local(repo)?;
    let current = ctx.store.current();

    // A named MR is dropped whatever its state — the "I'm done with this one,
    // reclaim its worktree and store even though it hasn't merged" path.
    if let Some(handle) = &args.mr {
        let id = parse_mr_handle(handle)?;
        return prune_one(&ctx, &id, "requested", &current);
    }

    // Otherwise sweep: terminal MRs always, plus dormant ones under a cutoff.
    // `--older-than` is a day count or an ISO-8601 date, as a Unix instant.
    let cutoff = args.older_than.as_deref().map(parse_cutoff).transpose()?;
    let mut pruned = 0;
    for info in ctx.store.list_infos() {
        let terminal = matches!(info.mr.state, MrState::Merged | MrState::Closed);
        // Dormant iff we have a real last-sync time (a full fetch, `fetched_at`
        // > 0) that predates the cutoff. A feed-only entry (`0`) is never dormant.
        let stale = cutoff.is_some_and(|before| info.fetched_at > 0 && info.fetched_at < before);
        if !terminal && !stale {
            continue;
        }
        let why = if terminal {
            state_word(info.mr.state, info.mr.draft)
        } else {
            "dormant"
        };
        prune_one(&ctx, &info.mr.id, why, &current)?;
        pruned += 1;
    }

    if pruned == 0 {
        log::info!("nothing to prune");
    } else {
        log::info!("pruned {pruned} MR(s)");
    }
    Ok(())
}

/// Drop one MR's whole local footprint: its review worktree (if the default one
/// is present), its snapshot pins (so git can GC the objects), and its store
/// directory — clearing the current-checkout pointer when it named this MR.
fn prune_one(ctx: &ReviewCtx, id: &str, why: &str, current: &Option<String>) -> Result<()> {
    remove_review_worktree(ctx, id);
    for (name, _) in ctx.repo.refs_under(&refs::mr_prefix(id)) {
        if let Err(e) = ctx.repo.delete_ref(&name) {
            log::warn!("MR {id}: could not delete {name}: {e}");
        }
    }
    ctx.store.delete_mr(id)?;
    // If we just pruned the checked-out MR, drop the dangling pointer so a
    // later `--next`/`--prev` doesn't navigate from a store that's gone.
    if current.as_deref() == Some(id) {
        ctx.store.clear_current()?;
    }
    log::info!("pruned MR {id} ({why})");
    Ok(())
}

/// Remove the MR's default review worktree if present — best-effort. A
/// `--worktree <custom>` checkout isn't tracked, so only the default sibling
/// path (`../<repo>.review/mr-<id>`) is reclaimed automatically.
fn remove_review_worktree(ctx: &ReviewCtx, id: &str) {
    let Some(toplevel) = ctx.repo.toplevel() else {
        return;
    };
    let dir = default_worktree_dir(&toplevel, &ctx.target.repo, id);
    if !dir.exists() {
        return;
    }
    match ctx.repo.worktree_remove(&dir, true) {
        Ok(()) => log::info!("removed worktree {}", dir.display()),
        Err(e) => log::warn!("MR {id}: could not remove worktree {}: {e}", dir.display()),
    }
}

/// Interpret `--older-than` and return the Unix instant before which an MR
/// counts as dormant. Accepts a relative age — `<N>`, `<N>d` (days), `<N>w`
/// (weeks) — or an absolute ISO-8601 date (`YYYY-MM-DD`).
///
/// A bare, unit-less number that is *shaped* like a year (four digits) is
/// rejected rather than read as "that many days ago": `--older-than 2026` is
/// almost always a mistyped date, and silently meaning ~5.5 years is worse than
/// a clear error asking for `2026d` or `2026-01-01`.
fn parse_cutoff(spec: &str) -> Result<i64> {
    let spec = spec.trim();

    // A dash means an absolute date; nothing else here contains one.
    if spec.contains('-') {
        let epoch_day = iso_date_to_epoch_day(spec)
            .with_context(|| format!("--older-than: '{spec}' is not a valid YYYY-MM-DD date"))?;
        return Ok(epoch_day * 86_400);
    }

    let (digits, per) = match spec.strip_suffix(['d', 'D']) {
        Some(n) => (n, 86_400),
        None => match spec.strip_suffix(['w', 'W']) {
            Some(n) => (n, 7 * 86_400),
            None => (spec, 86_400),
        },
    };
    let had_unit = digits.len() != spec.len();
    let n: i64 = digits.trim().parse().map_err(|_| {
        anyhow::anyhow!(
            "--older-than must be <days>, <N>d, <N>w, or a YYYY-MM-DD date, got '{spec}'"
        )
    })?;
    if !had_unit && (1000..=9999).contains(&n) {
        bail!(
            "--older-than '{spec}' is ambiguous (a bare four-digit number reads as a year, \
             not a day count); write '{n}d' for days or a full YYYY-MM-DD date"
        );
    }
    Ok(super::now_secs() - n.saturating_mul(per))
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cutoff_disambiguates_days_units_and_dates() {
        let now = super::super::now_secs();
        // Bare count and explicit `d` agree, and are in the past.
        let bare = parse_cutoff("30").unwrap();
        let dayed = parse_cutoff("30d").unwrap();
        assert_eq!(bare, dayed);
        assert!(bare <= now && bare >= now - 31 * 86_400);
        // Weeks scale by 7.
        assert_eq!(parse_cutoff("2w").unwrap(), parse_cutoff("14d").unwrap());
        // A year-shaped bare number is refused; the same value with a unit is fine.
        assert!(parse_cutoff("2026").is_err());
        assert!(parse_cutoff("2026d").is_ok());
        // An ISO date is absolute.
        assert_eq!(parse_cutoff("1970-01-02").unwrap(), 86_400);
        assert!(parse_cutoff("2026-13-01").is_err());
    }

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
