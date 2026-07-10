//! `wits review diff` — the diff's *coordinates*, not its rendering.
//!
//! The tool does not render diffs (the editor and `git` do that well); it owns
//! the coordinate layer — the snapshot SHAs, the commit list, and the changed
//! files a comment may anchor in. `--patch` is a terminal/debug convenience that
//! shells to `git`; `--json` is the coordinate payload an editor consumes.

use anyhow::{Context, Result};
use serde::Serialize;

use wits_util::git::Repository;

use super::model::{StoredCommit, StoredFile, SCHEMA};
use super::{local, DiffArgs};

#[derive(Serialize)]
struct DiffView {
    schema: u32,
    mr: String,
    range: String,
    base_sha: String,
    head_sha: String,
    commits: Vec<StoredCommit>,
    files: Vec<StoredFile>,
}

pub fn run(repo: &Repository, args: &DiffArgs) -> Result<()> {
    let ctx = local(repo)?;
    let id = super::parse_mr_handle(&args.mr)?;
    let info = ctx.store.load_info(&id).with_context(|| {
        format!("MR {id} isn't in the store yet — run `wits review fetch {id}` first")
    })?;

    // `all` is the whole reviewed range; anything else is passed to git verbatim.
    let range = if args.range == "all" {
        format!("{}..{}", info.version.base_sha, info.version.head_sha)
    } else {
        args.range.clone()
    };

    if args.patch {
        match ctx.repo.diff_patch(&range, None) {
            Some(patch) => println!("{patch}"),
            None => {
                anyhow::bail!("could not compute a diff for '{range}' (are the objects fetched?)")
            }
        }
        return Ok(());
    }

    let commits = ctx
        .repo
        .commits(&range)
        .into_iter()
        .map(|c| StoredCommit {
            sha: c.hash,
            subject: c.subject,
        })
        .collect();
    let files = ctx
        .repo
        .changed_files(&range)
        .into_iter()
        .map(|f| StoredFile {
            path: f.path,
            old_path: f.old_path,
            status: f.status.to_string(),
        })
        .collect();

    let view = DiffView {
        schema: SCHEMA,
        mr: id,
        range,
        base_sha: info.version.base_sha,
        head_sha: info.version.head_sha,
        commits,
        files,
    };

    if args.json {
        println!("{}", serde_json::to_string_pretty(&view)?);
    } else {
        println!("{} {}", view.mr, view.range);
        for c in &view.commits {
            println!("  {} {}", &c.sha[..c.sha.len().min(8)], c.subject);
        }
        for f in &view.files {
            println!("  {} {}", f.status, f.path);
        }
    }
    Ok(())
}
