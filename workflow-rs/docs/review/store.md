# `wits review` — the store and how to move it

`wits review` keeps its state in plain JSON files and a small set of git refs.
This document is the reference for that layout: what lives where, what each file
holds, and how to carry an in-progress review to another machine.

> The store's shape is a private implementation detail as far as *reading* review
> state goes — editors and scripts read through [`--json`](json.md), never these
> files. This document exists for the other reasons you'd want to know the
> layout: debugging, backup, and migration.

## Where the store lives

The base directory is resolved on a three-rung ladder, first hit wins:

1. `$WITS_REVIEW_DIR` — an explicit override.
2. `$XDG_STATE_HOME/wits/review` — when `XDG_STATE_HOME` is set.
3. `$GIT_DIR/wits/review` — the default, per-clone, alongside `.git/machete`.

State (this store) is kept separate from config (the feed
[`review.toml`](../review.md#feeds), which is `$XDG_CONFIG_HOME`-based) — the
correct split, and it keeps a backup of one from dragging in the other.

Under the base, each repo gets its own subtree, keyed by the target remote's
identity so one central root (via `WITS_REVIEW_DIR`/`XDG_STATE_HOME`) can hold
many repos without collision:

```
<base>/<host>/<owner>/<repo>/
├── remote/
│   └── mr-<id>.json      # the forge's state, as last fetched (a cache)
├── draft/
│   └── mr-<id>.json      # your unsubmitted review (the precious part)
└── current               # the MR the last `checkout` materialized
```

For a GitLab nested group the `<owner>` segment itself contains slashes and
becomes nested directories — that is fine.

## `remote/mr-<id>.json` — the cache

The forge's state for one MR as we last saw it: refetchable at will, safe to
delete or overwrite whole. This is where everyone else's comments live.

```json
{
  "schema": 1,
  "mr": { "id": "123", "display": "#123", "state": "open", "draft": false,
          "title": "…", "author": "alice", "base": "main", "source": "fix-lock",
          "head_sha": "9f8e…", "updated_at": "2026-07-01T12:00:00Z",
          "labels": [], "web_url": "https://…/123" },
  "version": { "base_sha": "aaaa…", "start_sha": "aaaa…", "head_sha": "9f8e…" },
  "fetched_at": "1719830400",
  "commits": [ { "sha": "…", "subject": "…" } ],
  "files":   [ { "path": "src/x.c", "old_path": null, "status": "M" } ],
  "threads": [ /* remote threads — same shape as show's JSON */ ]
}
```

- `version` holds the three diff SHAs a comment anchors against (`start`/`base`
  coincide on GitHub; GitLab uses all three).
- `fetched_at` is a Unix timestamp, used by `prune --older-than`.
- `commits`/`files` are derived **locally** from the fetched objects, not taken
  from the forge.
- A **feed** fetch writes a light cache (metadata only — empty `threads`,
  `commits`, `files`, and an empty `version`); a full `fetch <mr>` fills the
  rest. A light refresh never discards an existing full cache's threads.

## `draft/mr-<id>.json` — the draft

Your unsubmitted review for one MR: a verdict, an optional summary, and an
ordered list of pending actions. This is the only state that would be *lost* —
the cache can always be refetched — so it is the thing migration moves.

```json
{
  "schema": 1,
  "verdict": "request-changes",
  "summary": "A few blockers.",
  "actions": [
    { "action": "comment", "id": "local:1",
      "placement": { "kind": "line", "path": "src/x.c", "side": "new", "line": 50, "commit": "9f8e…" },
      "body": "Agreed." },
    { "action": "reply",   "id": "local:2", "thread": "9987", "body": "Done." },
    { "action": "resolve", "thread": "9987", "resolved": true }
  ],
  "seq": 2
}
```

`seq` is the monotonic local-id counter (`local:<seq>`), never reused after a
drop. The file is **deleted** the moment the draft empties — an empty draft and
no draft are the same thing. `submit` clears the actions it lands; a fully
flushed draft's file disappears and the MR is re-fetched.

The field shapes match the [`draft --json`](json.md#draft-mr---json---the-pending-draft)
and [thread](json.md#threads) documentation.

## Git refs — pinning reviewed objects

Fetching an MR pulls its objects and holds them alive with the tool's own refs,
so a later force-push on the author's side can't garbage-collect the snapshot you
reviewed:

```
refs/wits/review/<mr>/<snapshot-sha>        → the reviewed head commit
refs/wits/review/<mr>/<snapshot-sha>-base   → its base, when not an ancestor of head
```

The names carry only what disambiguates within one clone — the MR number and the
SHA. These refs *are* the record of which snapshots you have pinned; enumerate
them with `git for-each-ref refs/wits/review/`. `prune` deletes them (letting git
collect the objects) once an MR is terminal or dormant. They are created
unconditionally and owe nothing to `git-branchless` or any other tool.

## Moving a review to another machine

"Sharing" here means carrying *your own* in-progress review between *your own*
machines — the forge is the collaboration layer, not this store.

Because the cache is refetchable, only the **draft** needs to travel:

```sh
# on the first machine — copy the drafts for a repo
src=$GIT_DIR/wits/review/github.com/me/proj/draft
cp -r "$src" /media/key/proj-drafts

# on the second machine — drop them in and refetch the context
dst=$GIT_DIR/wits/review/github.com/me/proj
mkdir -p "$dst" && cp -r /media/key/proj-drafts "$dst/draft"
wits review fetch 123        # rebuild the cache and pin the objects
wits review show 123         # your pending comments are back, merged in
```

The draft references the reviewed snapshot's SHA, so the second machine must be
able to `fetch` the same MR (it can — the MR still exists on the forge). If you
keep a single central store via `WITS_REVIEW_DIR` on shared/synced storage, both
machines share the drafts automatically and only the pins (which are per-clone
git refs) are rebuilt by `fetch`.

## Schema versioning

Every file and every `--json` payload carries an integer `schema` (currently
`1`). When the shape changes incompatibly it is bumped; a reader that sees a
schema it doesn't recognize should refuse rather than guess. Because the cache is
disposable, a schema bump can be handled for the cache by simply refetching; only
draft migrations (if ever needed) warrant more care.
