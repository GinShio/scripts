//! `wits review show` / `draft` — the read path and the stable `--json` contract.
//!
//! `show` with no MR is the inbox; with an MR it is the detail view, which folds
//! the pending draft (`local.json`) into the remote discussion (`comments.json`)
//! so the editor sees one merged picture. Filtering is the knob for large MRs —
//! the payload is always the whole MR, never paginated.

use anyhow::{Context, Result};
use serde::Serialize;

use wits_util::git::Repository;

use super::model::{
    bare_thread_id, short, Action, Comment, Local, MrInfo, Placement, Snapshot, StoredCommit,
    StoredFile, Thread, SCHEMA,
};
use super::{local, ShowArgs};

#[derive(Serialize)]
struct SnapshotView {
    base_sha: String,
    head_sha: String,
}

#[derive(Serialize)]
struct Neighbors {
    position: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    prev_mr: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    next_mr: Option<String>,
    nodes: Vec<String>,
}

#[derive(Serialize)]
struct DraftView {
    #[serde(skip_serializing_if = "Option::is_none")]
    verdict: Option<wits_util::forge::Verdict>,
    #[serde(skip_serializing_if = "Option::is_none")]
    summary: Option<String>,
    pending: usize,
}

#[derive(Serialize)]
struct DetailView {
    schema: u32,
    mr: MrInfo,
    snapshot: SnapshotView,
    /// The full snapshot history, oldest first, so the editor can offer
    /// switching (`diff --snapshot <sha>`).
    snapshots: Vec<Snapshot>,
    neighbors: Neighbors,
    commits: Vec<StoredCommit>,
    files: Vec<StoredFile>,
    threads: Vec<Thread>,
    draft: DraftView,
}

#[derive(Serialize)]
struct InboxReview {
    pending: usize,
}

#[derive(Serialize)]
struct InboxItem {
    #[serde(flatten)]
    mr: MrInfo,
    review: InboxReview,
}

#[derive(Serialize)]
struct InboxView {
    schema: u32,
    items: Vec<InboxItem>,
}

pub fn run(repo: &Repository, args: &ShowArgs) -> Result<()> {
    let ctx = local(repo)?;
    match &args.mr {
        Some(handle) => show_detail(&ctx, &super::parse_mr_handle(handle)?, args),
        None => show_inbox(&ctx, args),
    }
}

fn show_detail(ctx: &super::Local, id: &str, args: &ShowArgs) -> Result<()> {
    let info = ctx.store.load_info(id).with_context(|| {
        format!("MR {id} isn't in the store yet — run `wits review fetch {id}` first")
    })?;
    let comments = ctx.store.load_comments(id);
    let draft = ctx.store.load_local(id)?;

    let infos = ctx.store.list_infos();
    let (nodes, position) = super::stack_chain(&infos, id);
    let neighbors = Neighbors {
        position,
        prev_mr: position.checked_sub(1).and_then(|i| nodes.get(i).cloned()),
        next_mr: nodes.get(position + 1).cloned(),
        nodes,
    };

    let mut threads = merge_threads(comments.threads, &draft, info.head());
    apply_filters(&mut threads, args);

    let view = DetailView {
        schema: SCHEMA,
        snapshot: SnapshotView {
            base_sha: info
                .current()
                .map(|s| s.base_sha.clone())
                .unwrap_or_default(),
            head_sha: info.head().to_owned(),
        },
        snapshots: info.snapshots.clone(),
        neighbors,
        commits: info.commits.clone(),
        files: info.files.clone(),
        threads,
        draft: DraftView {
            verdict: draft.verdict,
            summary: draft.summary.clone(),
            pending: pending_count(&draft),
        },
        mr: info.mr,
    };

    if args.json {
        println!("{}", serde_json::to_string_pretty(&view)?);
    } else {
        print_detail_human(&view);
    }
    Ok(())
}

/// Fold the draft into the remote threads: new comments become local threads,
/// replies attach to their remote thread as pending comments, resolutions flip
/// the flag. Pending items get a synthetic `local:<n>` id for display.
fn merge_threads(mut threads: Vec<Thread>, draft: &Local, head: &str) -> Vec<Thread> {
    for (i, action) in draft.actions.iter().enumerate() {
        let local_id = format!("local:{i}");
        match action {
            Action::Comment { body, .. } => {
                let placement = action.placement(head).unwrap_or(Placement::Mr);
                threads.push(Thread {
                    id: local_id.clone(),
                    origin: "local".into(),
                    resolved: false,
                    outdated: false,
                    placement,
                    comments: vec![pending_comment(&local_id, body)],
                });
            }
            Action::Reply { thread, body } => {
                let target = format!("remote:{}", bare_thread_id(thread));
                if let Some(t) = threads.iter_mut().find(|t| t.id == target) {
                    t.comments.push(pending_comment(&local_id, body));
                }
            }
            Action::Resolve { thread, resolved } => {
                let target = format!("remote:{}", bare_thread_id(thread));
                if let Some(t) = threads.iter_mut().find(|t| t.id == target) {
                    t.resolved = *resolved;
                }
            }
        }
    }
    threads
}

fn pending_comment(id: &str, body: &str) -> Comment {
    Comment {
        id: id.to_owned(),
        author: "@me".into(),
        origin: "local".into(),
        body: body.to_owned(),
        created_at: String::new(),
        state: "pending".into(),
    }
}

fn apply_filters(threads: &mut Vec<Thread>, args: &ShowArgs) {
    if args.outdated {
        threads.retain(|t| t.outdated);
    }
    if args.resolved {
        threads.retain(|t| t.resolved);
    }
    if args.unresolved {
        threads.retain(|t| !t.resolved);
    }
    if args.unread {
        threads.retain(|t| t.comments.last().is_some_and(|c| c.origin == "remote"));
    }
    if let Some(path) = &args.file {
        threads.retain(|t| placement_path(&t.placement) == Some(path.as_str()));
    }
}

fn placement_path(p: &Placement) -> Option<&str> {
    match p {
        Placement::Line { path, .. } | Placement::File { path, .. } => Some(path),
        Placement::Mr => None,
    }
}

fn pending_count(draft: &Local) -> usize {
    draft.actions.len() + usize::from(draft.verdict.is_some() || draft.summary.is_some())
}

fn show_inbox(ctx: &super::Local, args: &ShowArgs) -> Result<()> {
    let mut infos = ctx.store.list_infos();
    infos.sort_by(|a, b| b.mr.updated_at.cmp(&a.mr.updated_at));

    let items: Vec<InboxItem> = infos
        .into_iter()
        .map(|info| {
            // One MR's corrupt draft shouldn't sink the rest of the inbox —
            // degrade to "no pending actions" with a per-MR warning.
            let pending = match ctx.store.load_local(&info.mr.id) {
                Ok(draft) => pending_count(&draft),
                Err(e) => {
                    log::warn!("MR {}: skipping draft in inbox: {e}", info.mr.id);
                    0
                }
            };
            InboxItem {
                mr: info.mr,
                review: InboxReview { pending },
            }
        })
        .collect();

    if args.json {
        println!(
            "{}",
            serde_json::to_string_pretty(&InboxView {
                schema: SCHEMA,
                items
            })?
        );
    } else if items.is_empty() {
        println!(
            "(nothing fetched — `wits review fetch <mr>` or `wits review fetch --feed <name>`)"
        );
    } else {
        for item in &items {
            let pending = if item.review.pending > 0 {
                format!("  pending:{}", item.review.pending)
            } else {
                String::new()
            };
            println!(
                "{:<7} [{}] {}  ({}){pending}",
                item.mr.display, item.mr.state, item.mr.title, item.mr.author
            );
        }
    }
    Ok(())
}

fn print_detail_human(view: &DetailView) {
    println!("{} [{}] {}", view.mr.display, view.mr.state, view.mr.title);
    println!(
        "  by {} · base {} · {}",
        view.mr.author, view.mr.base, view.mr.web_url
    );
    println!(
        "  snapshot {}..{}",
        short(&view.snapshot.base_sha),
        short(&view.snapshot.head_sha)
    );
    if view.neighbors.nodes.len() > 1 {
        println!("  stack: {}", view.neighbors.nodes.join(" -> "));
    }
    if !view.files.is_empty() {
        println!("  files:");
        for f in &view.files {
            println!("    {} {}", f.status, f.path);
        }
    }
    if view.threads.is_empty() {
        println!("  (no threads)");
    } else {
        println!("  threads:");
        for t in &view.threads {
            let flags = [
                (t.resolved, "resolved"),
                (t.outdated, "outdated"),
                (t.origin == "local", "pending"),
            ]
            .iter()
            .filter(|(on, _)| *on)
            .map(|(_, s)| *s)
            .collect::<Vec<_>>()
            .join(",");
            let flags = if flags.is_empty() {
                String::new()
            } else {
                format!(" [{flags}]")
            };
            println!("    {} {}{flags}", t.id, describe_placement(&t.placement));
            for c in &t.comments {
                println!("      {} ({}): {}", c.author, c.origin, first_line(&c.body));
            }
        }
    }
    if view.draft.pending > 0 {
        let verdict = view
            .draft
            .verdict
            .map(|v| format!(" verdict={}", v.display_str()))
            .unwrap_or_default();
        println!("  draft: {} pending action(s){verdict}", view.draft.pending);
    }
}

fn describe_placement(p: &Placement) -> String {
    match p {
        Placement::Line {
            path, end, start, ..
        } => {
            let span = match start {
                Some(s) => format!("{}-{}", s.line, end.line),
                None => end.line.to_string(),
            };
            format!("{path}:{span} ({})", end.side.as_str())
        }
        Placement::File { path, .. } => format!("{path} (file)"),
        Placement::Mr => "(conversation)".to_owned(),
    }
}

fn first_line(s: &str) -> &str {
    s.lines().next().unwrap_or("")
}

/// Read a JSON batch (`{verdict?, summary?, actions:[…]}`) from a file or stdin
/// and append its actions to the draft, setting the verdict/summary when the
/// batch provides them. The tool owns the write; the editor only provides the
/// content. Surgery on a queued action is done by editing `local.json`.
fn ingest(ctx: &super::Local, id: &str, input: &std::path::Path) -> Result<()> {
    use std::io::Read;
    let text = if input.as_os_str() == "-" {
        let mut buf = String::new();
        std::io::stdin()
            .read_to_string(&mut buf)
            .context("reading the draft batch from stdin")?;
        buf
    } else {
        std::fs::read_to_string(input).with_context(|| format!("reading {}", input.display()))?
    };
    let batch: Local = serde_json::from_str(&text).context("parsing the draft batch as JSON")?;
    if batch.schema != SCHEMA {
        anyhow::bail!(
            "draft batch schema {} is unsupported (expected {}). The `local.json` \
             contract has likely changed — regenerate the batch with the current shape.",
            batch.schema,
            SCHEMA
        );
    }

    let mut draft = ctx.store.load_local(id)?;
    let added = batch.actions.len();

    // Stamp each incoming comment's `commit` with the current snapshot head, so
    // the comment is anchored to the snapshot it was written against. Actions
    // that already carry a `commit` (explicit hand-edit) are left as-is.
    let head = ctx
        .store
        .load_info(id)
        .map(|i| i.head().to_owned())
        .unwrap_or_default();
    let mut actions = batch.actions;
    for action in &mut actions {
        if let Action::Comment { ref mut commit, .. } = action {
            if commit.is_none() && !head.is_empty() {
                *commit = Some(head.clone());
            }
        }
    }
    draft.actions.extend(actions);
    if batch.verdict.is_some() {
        draft.verdict = batch.verdict;
    }
    if batch.summary.is_some() {
        draft.summary = batch.summary;
    }
    ctx.store.save_local(id, &draft)?;
    log::info!("appended {added} action(s) to MR {id}'s draft");
    Ok(())
}

pub fn run_draft(repo: &Repository, args: &super::DraftArgs) -> Result<()> {
    let ctx = local(repo)?;
    let id = super::parse_mr_handle(&args.mr)?;

    // With an input (file or `-`), ingest a batch into the draft (the tool owns
    // the write); otherwise show the current draft.
    if let Some(input) = &args.input {
        return ingest(&ctx, &id, input);
    }

    let draft = ctx.store.load_local(&id)?;

    if args.json {
        println!("{}", serde_json::to_string_pretty(&draft)?);
        return Ok(());
    }
    if draft.is_empty() {
        println!("(no pending draft for MR {id})");
        return Ok(());
    }
    if let Some(v) = draft.verdict {
        println!("verdict: {}", v.display_str());
    }
    if let Some(s) = &draft.summary {
        println!("summary: {}", first_line(s));
    }
    for (i, action) in draft.actions.iter().enumerate() {
        match action {
            Action::Comment { body, commit, .. } => {
                let where_ = describe_placement(&action.placement("").unwrap_or(Placement::Mr));
                let at = commit
                    .as_deref()
                    .map(|s| format!(" @{}", short(s)))
                    .unwrap_or_default();
                println!("  local:{i}  comment {where_}{at}  {}", first_line(body));
            }
            Action::Reply { thread, body } => {
                println!(
                    "  local:{i}  reply -> remote:{}  {}",
                    bare_thread_id(thread),
                    first_line(body)
                )
            }
            Action::Resolve { thread, resolved } => {
                let verb = if *resolved { "resolve" } else { "unresolve" };
                println!("  local:{i}  {verb} remote:{}", bare_thread_id(thread))
            }
        }
    }
    Ok(())
}
