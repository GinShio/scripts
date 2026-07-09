//! The authoring verbs — `comment` (new/reply/edit), `verdict`, `drop`,
//! `resolve`/`unresolve`. Every one of these only mutates the local draft; none
//! touches the network. That is the whole point of the draft: a reviewer builds
//! up a set of actions and `submit` flushes them as one batch (one notification
//! where the platform allows), rather than each keystroke pinging the forge.

use anyhow::{bail, Context, Result};

use wits_util::forge::Side;
use wits_util::git::Repository;

use super::model::{Action, Placement};
use super::{local, CommentArgs, DropArgs, ThreadArgs, VerdictArgs};

pub fn run(repo: &Repository, args: &CommentArgs) -> Result<()> {
    let ctx = local(repo)?;
    let id = super::parse_mr_handle(&args.mr)?;
    let mut draft = ctx.store.load_draft(&id);

    // --edit changes an existing pending body; every other mode adds an action.
    if let Some(target) = &args.edit {
        let body = super::read_body(args.body.as_deref())?;
        if !draft.edit_body(target, body) {
            bail!("no pending action '{target}' in MR {id}'s draft");
        }
        ctx.store.save_draft(&id, &draft)?;
        log::info!("edited {target}");
        return Ok(());
    }

    let body = super::read_body(args.body.as_deref())?;

    // A reply needs no snapshot; a code anchor needs the reviewed head SHA.
    let action = if let Some(thread) = &args.reply {
        let new_id = draft.next_id();
        let thread = strip_remote(thread);
        Action::Reply {
            id: new_id,
            thread,
            body,
        }
    } else if args.mr_level {
        Action::Comment {
            id: draft.next_id(),
            placement: Placement::Mr,
            body,
        }
    } else {
        let commit = reviewed_head(&ctx, &id)?;
        let placement = if let Some(spec) = &args.line {
            parse_line(spec, args.start_line, commit)?
        } else if let Some(path) = &args.file {
            Placement::File {
                path: path.clone(),
                commit: Some(commit),
            }
        } else {
            bail!("choose a placement: --line, --file, --mr, --reply, or --edit");
        };
        Action::Comment {
            id: draft.next_id(),
            placement,
            body,
        }
    };

    let new_id = action.id().unwrap_or_default().to_owned();
    draft.actions.push(action);
    ctx.store.save_draft(&id, &draft)?;
    log::info!("recorded {new_id} (submit with `wits review submit {id}`)");
    Ok(())
}

/// The head SHA of the snapshot under review, which a code anchor is stamped
/// with so it submits against what was reviewed. Requires a full fetch.
fn reviewed_head(ctx: &super::Local, id: &str) -> Result<String> {
    let cache = ctx
        .store
        .load_cache(id)
        .with_context(|| format!("MR {id} isn't fetched — run `wits review fetch {id}` first"))?;
    if cache.version.head_sha.is_empty() {
        bail!(
            "MR {id} has no reviewed snapshot yet — run `wits review fetch {id}` for full detail"
        );
    }
    Ok(cache.version.head_sha)
}

/// Parse a `PATH:LINE[:old|new]` anchor spec. Side defaults to `new` (the
/// post-image); `old` is for a line a change deleted.
fn parse_line(spec: &str, start_line: Option<u32>, commit: String) -> Result<Placement> {
    let (rest, side) = match spec.rsplit_once(':') {
        Some((r, "old")) => (r, Side::Old),
        Some((r, "new")) => (r, Side::New),
        _ => (spec, Side::New),
    };
    let (path, line) = rest
        .rsplit_once(':')
        .with_context(|| format!("expected PATH:LINE[:side], got '{spec}'"))?;
    let line: u32 = line
        .parse()
        .with_context(|| format!("LINE must be a number, got '{line}'"))?;
    if path.is_empty() {
        bail!("empty path in '{spec}'");
    }
    Ok(Placement::Line {
        path: path.to_owned(),
        old_path: None,
        side,
        line,
        start_line,
        commit: Some(commit),
    })
}

/// Strip a `remote:` prefix off a thread id so the stored action holds the bare
/// forge id the reply/resolve primitives expect.
fn strip_remote(thread: &str) -> String {
    thread.strip_prefix("remote:").unwrap_or(thread).to_owned()
}

pub fn run_verdict(repo: &Repository, args: &VerdictArgs) -> Result<()> {
    let ctx = local(repo)?;
    let id = super::parse_mr_handle(&args.mr)?;
    let mut draft = ctx.store.load_draft(&id);

    draft.verdict = Some(args.verdict.into());
    // A summary is read only when a body source is given, so a bare verdict
    // doesn't block waiting on stdin.
    if args.body.is_some() {
        draft.summary = Some(super::read_body(args.body.as_deref())?);
    }
    ctx.store.save_draft(&id, &draft)?;
    log::info!("verdict set for MR {id}");
    Ok(())
}

pub fn run_drop(repo: &Repository, args: &DropArgs) -> Result<()> {
    let ctx = local(repo)?;
    let id = super::parse_mr_handle(&args.mr)?;

    if args.id.starts_with("remote:") {
        bail!(
            "v1 can only drop pending local actions; editing or deleting a published \
             comment ('{}') isn't supported yet",
            args.id
        );
    }

    let mut draft = ctx.store.load_draft(&id);
    if !draft.remove(&args.id) {
        bail!("no pending action '{}' in MR {id}'s draft", args.id);
    }
    ctx.store.save_draft(&id, &draft)?;
    log::info!("dropped {}", args.id);
    Ok(())
}

pub fn run_resolve(repo: &Repository, args: &ThreadArgs, resolved: bool) -> Result<()> {
    let ctx = local(repo)?;
    let id = super::parse_mr_handle(&args.mr)?;
    let mut draft = ctx.store.load_draft(&id);

    draft.set_resolved(strip_remote(&args.thread), resolved);
    ctx.store.save_draft(&id, &draft)?;
    let verb = if resolved { "resolve" } else { "unresolve" };
    log::info!("recorded {verb} of {} (GitLab only in v1)", args.thread);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_line_specs_with_and_without_side() {
        let commit = "abc".to_owned();
        match parse_line("src/x.c:42", None, commit.clone()).unwrap() {
            Placement::Line {
                path, line, side, ..
            } => {
                assert_eq!(path, "src/x.c");
                assert_eq!(line, 42);
                assert_eq!(side, Side::New);
            }
            _ => panic!("expected a line placement"),
        }
        match parse_line("src/x.c:10:old", Some(8), commit).unwrap() {
            Placement::Line {
                side,
                line,
                start_line,
                ..
            } => {
                assert_eq!(side, Side::Old);
                assert_eq!(line, 10);
                assert_eq!(start_line, Some(8));
            }
            _ => panic!("expected a line placement"),
        }
        assert!(parse_line("noline", None, "abc".into()).is_err());
    }
}
