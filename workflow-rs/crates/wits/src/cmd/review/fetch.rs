//! `wits review fetch` — bring the local store in line with the forge.
//!
//! Idempotent, like `git fetch`. Fetching one MR is a *full* pull: metadata and
//! diff state into `info.json`, the discussion into `comments.json`, and the
//! objects pinned by a `refs/wits/review/*` ref so a later force-push can't GC
//! them. Fetching a feed is *light* — it refreshes only `info.json` for each
//! matching MR, leaving the expensive per-MR pull to `fetch <mr>`. With no
//! argument, every configured feed is refreshed (the RSS "refresh all").

use std::collections::{BTreeSet, HashSet};

use anyhow::{Context, Result};

use wits_util::forge::{DiffVersion, MergeRequest, MrSummary};
use wits_util::git::Repository;

use super::config::{self, Config};
use super::model::{range_artifacts, Comments, Info, StoredCommit, StoredFile, Thread, SCHEMA};
use super::store::{refs, Store};
use super::{now_secs, online, parse_mr_handle, Online};

pub fn run(repo: &Repository, args: &super::FetchArgs) -> Result<()> {
    let ctx = online(repo)?;

    if let Some(handle) = &args.mr {
        let id = parse_mr_handle(handle)?;
        let remote = target_remote(&ctx);
        return fetch_mr(&ctx, &remote, &id, !args.no_stack);
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

/// Fetch one MR and, unless `only_seed`, every other MR in its stack.
///
/// A stack is the review unit, so fetching an MR pulls its whole stack by
/// default — no flag, no half-fetched stack left behind by a label/limit feed.
/// The members are discovered on the forge by walking the seed's base/source
/// links (see [`stack_member_ids`]); each gets its own full [`fetch_one`]. The
/// walk is bounded to the actual stack — it never enumerates a trunk's MRs — so
/// this is safe to do automatically.
fn fetch_mr(ctx: &Online, remote: &str, seed_id: &str, complete_stack: bool) -> Result<()> {
    let noun = ctx.forge.noun();
    fetch_one(ctx, remote, seed_id)?;

    if !complete_stack {
        log::info!("fetched {noun} {seed_id}");
        return Ok(());
    }

    // Discover the rest of the stack from the seed's now-stored base/source, so
    // we reuse the fetch we just did rather than asking the forge for the seed
    // again.
    let Some(info) = ctx.local.store.load_info(seed_id) else {
        log::info!("fetched {noun} {seed_id}");
        return Ok(());
    };
    let forge = ctx.forge.as_ref();
    let ids = discover_stack(
        [(
            seed_id.to_owned(),
            info.mr.base.clone(),
            info.mr.source.clone(),
        )],
        |branch| forge.find_any(branch),
        |branch| forge.find_children(branch),
    )?;

    let mut extra = 0;
    for id in ids.iter().filter(|id| id.as_str() != seed_id) {
        fetch_one(ctx, remote, id)?;
        extra += 1;
    }
    if extra > 0 {
        log::info!("fetched {noun} {seed_id} and {extra} more in its stack");
    } else {
        log::info!("fetched {noun} {seed_id}");
    }
    Ok(())
}

/// The ids of every MR in the stack(s) containing the given seeds, each a
/// `(id, base, source)` triple. Discovered by a breadth-first walk of the
/// base/source links: `parent_of(base)` climbs toward the trunk, and
/// `children_of(source)` descends toward the leaves. The walk is bounded to the
/// real stack — a trunk is nobody's source (so the upward climb stops) and we
/// only ever ask for the children *of a source branch*, never of a trunk.
///
/// Seeding from many MRs at once (a whole feed) shares one visited set, so a
/// stack several of them belong to is walked exactly once. Each branch is
/// probed at most once in each direction, and `BTreeSet` gives a stable,
/// id-sorted result. Pure over the two link functions, so the graph logic is
/// testable without a forge.
fn discover_stack(
    seeds: impl IntoIterator<Item = (String, String, String)>,
    parent_of: impl Fn(&str) -> Result<Option<MergeRequest>>,
    children_of: impl Fn(&str) -> Result<Vec<MergeRequest>>,
) -> Result<Vec<String>> {
    let mut ids: BTreeSet<String> = BTreeSet::new();
    // Frontier of `(base, source)` links still to expand.
    let mut frontier = Vec::new();
    for (id, base, source) in seeds {
        if ids.insert(id) {
            frontier.push((base, source));
        }
    }

    // A branch need only be probed once per direction, even when several seeds
    // share the same stack.
    let mut climbed: HashSet<String> = HashSet::new();
    let mut descended: HashSet<String> = HashSet::new();

    while let Some((base, source)) = frontier.pop() {
        if !base.is_empty() && climbed.insert(base.clone()) {
            if let Some(parent) = parent_of(&base)? {
                if ids.insert(parent.id) {
                    frontier.push((parent.base, parent.source));
                }
            }
        }
        if !source.is_empty() && descended.insert(source.clone()) {
            for child in children_of(&source)? {
                if ids.insert(child.id) {
                    frontier.push((child.base, child.source));
                }
            }
        }
    }
    Ok(ids.into_iter().collect())
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
    for summary in &summaries {
        store_summary(store, summary.clone())?;
    }

    // Complete each matched MR's stack: a label/limit filter can match only part
    // of a stack, so we walk the base/source links and light-fetch (summary
    // only, no objects or threads) any member the filter missed. The inbox then
    // shows whole stacks; a full pull still waits for `fetch <mr>`.
    let forge = ctx.forge.as_ref();
    let seeds = summaries
        .iter()
        .map(|s| (s.id.clone(), s.base.clone(), s.source.clone()));
    let members = discover_stack(
        seeds,
        |branch| forge.find_any(branch),
        |branch| forge.find_children(branch),
    )?;
    let matched: HashSet<&str> = summaries.iter().map(|s| s.id.as_str()).collect();
    let mut added = 0;
    for id in members.iter().filter(|id| !matched.contains(id.as_str())) {
        // A member's summary alone (no diff state / discussion) is enough for the
        // inbox and stack navigation; `mr_details` is one metadata call.
        store_summary(store, forge.mr_details(id)?.summary)?;
        added += 1;
    }

    let extra = if added > 0 {
        format!(" (+{added} stack member(s))")
    } else {
        String::new()
    };
    log::info!(
        "feed '{name}': {} MR(s){extra} (run `wits review fetch <mr>` for full detail)",
        summaries.len()
    );
    Ok(())
}

/// Store an MR's summary as a light `info.json`: refresh `mr` and `fetched_at`,
/// preserving any snapshot history a prior full fetch recorded. Shared by a feed
/// refresh and stack completion, so the light-touch shape can't drift between
/// them.
fn store_summary(store: &Store, summary: MrSummary) -> Result<()> {
    let mut info = match store.load_info(&summary.id) {
        Some(mut existing) => {
            existing.mr = summary;
            existing
        }
        None => Info {
            schema: SCHEMA,
            mr: summary,
            snapshots: Vec::new(),
            fetched_at: 0,
            commits: Vec::new(),
            files: Vec::new(),
        },
    };
    info.fetched_at = now_secs();
    store.save_info(&info.mr.id, &info)
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

#[cfg(test)]
mod tests {
    use super::*;
    use wits_util::forge::MrState;

    /// A tiny in-memory stack: `(id, base, source)` per MR. The link functions
    /// stand in for the forge — `parent_of(branch)` = the MR whose source is
    /// `branch`, `children_of(branch)` = the MRs whose base is `branch`.
    #[allow(clippy::type_complexity)]
    fn links(
        stack: &'static [(&'static str, &'static str, &'static str)],
    ) -> (
        impl Fn(&str) -> Result<Option<MergeRequest>>,
        impl Fn(&str) -> Result<Vec<MergeRequest>>,
    ) {
        let mk = |&(id, base, source): &(&str, &str, &str)| MergeRequest {
            id: id.to_owned(),
            display: format!("#{id}"),
            state: MrState::Open,
            base: base.to_owned(),
            source: source.to_owned(),
            head_sha: None,
            body: String::new(),
            web_url: String::new(),
        };
        let parent = move |branch: &str| {
            Ok(stack
                .iter()
                .find(|(_, _, source)| *source == branch)
                .map(mk))
        };
        let children = move |branch: &str| {
            Ok(stack
                .iter()
                .filter(|(_, base, _)| *base == branch)
                .map(mk)
                .collect())
        };
        (parent, children)
    }

    /// Discover from a single `(id, base, source)` seed — the `fetch <mr>` shape.
    fn from_seed(
        seed: (&str, &str, &str),
        parent: impl Fn(&str) -> Result<Option<MergeRequest>>,
        children: impl Fn(&str) -> Result<Vec<MergeRequest>>,
    ) -> Vec<String> {
        let (id, base, source) = seed;
        discover_stack(
            [(id.to_owned(), base.to_owned(), source.to_owned())],
            parent,
            children,
        )
        .unwrap()
    }

    #[test]
    fn discovers_the_whole_stack_from_any_seed() {
        // main <- a <- b <- c (a linear stack of three MRs).
        let stack: &[(&str, &str, &str)] = &[("1", "main", "a"), ("2", "a", "b"), ("3", "b", "c")];
        let (parent, children) = links(stack);
        // Seeding from the middle still finds both ends.
        assert_eq!(
            from_seed(("2", "a", "b"), &parent, &children),
            ["1", "2", "3"]
        );
    }

    #[test]
    fn discovers_both_arms_of_a_fork() {
        // main <- a, then a forks into b and c.
        let stack: &[(&str, &str, &str)] = &[
            ("1", "main", "a"),
            ("2", "a", "b"),
            ("3", "a", "c"),
            ("4", "c", "d"),
        ];
        let (parent, children) = links(stack);
        // From the base MR, every descendant on both arms is discovered.
        assert_eq!(
            from_seed(("1", "main", "a"), &parent, &children),
            ["1", "2", "3", "4"]
        );
        // And from a leaf, the whole tree is still reached via the shared trunk.
        assert_eq!(
            from_seed(("4", "c", "d"), &parent, &children),
            ["1", "2", "3", "4"]
        );
    }

    #[test]
    fn a_lone_mr_is_its_own_stack() {
        let stack: &[(&str, &str, &str)] = &[("9", "main", "solo")];
        let (parent, children) = links(stack);
        assert_eq!(from_seed(("9", "main", "solo"), &parent, &children), ["9"]);
    }

    #[test]
    fn many_seeds_in_one_stack_are_walked_once() {
        // A feed matched two MRs of the same three-MR stack. Seeding both must
        // yield the whole stack (the third member included) while probing each
        // branch only once — the shared visited set, not one walk per seed.
        let stack: &[(&str, &str, &str)] = &[("1", "main", "a"), ("2", "a", "b"), ("3", "b", "c")];
        let (parent, children) = links(stack);
        let probes = std::cell::Cell::new(0u32);
        let counted_children = |branch: &str| {
            probes.set(probes.get() + 1);
            children(branch)
        };
        let ids = discover_stack(
            [
                ("1".into(), "main".into(), "a".into()),
                ("2".into(), "a".into(), "b".into()),
            ],
            parent,
            counted_children,
        )
        .unwrap();
        assert_eq!(ids, ["1", "2", "3"]);
        // Three distinct source branches (a, b, c) => three children probes,
        // never one per seed.
        assert_eq!(probes.get(), 3, "each source branch probed exactly once");
    }
}
