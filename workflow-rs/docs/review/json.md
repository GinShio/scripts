# `wits review` — the JSON contract

This is the stable interface between `wits review` and an editor (or any other
front-end). The rule is simple:

- **Read through `--json`.** `show`, `diff`, and `draft` emit versioned JSON on
  stdout. This is the *only* supported way to read review state — the on-disk
  [store](store.md) is a private implementation detail and may change.
- **Write through subcommands.** To add a comment, set a verdict, resolve a
  thread, and so on, invoke the ordinary CLI verb; the body is passed on stdin.
  A front-end never writes the store directly and never POSTs JSON in.

Every payload carries an integer `schema` (currently `1`); bump-aware clients
should refuse a schema they don't understand.

## Conventions used throughout

- **Ids are origin-prefixed.** `remote:<forge-id>` for anything the forge owns;
  `local:<n>` for a pending draft action. A front-end addresses both forms back
  to the CLI verbatim (e.g. `--reply remote:9987`, `drop local:2`).
- **`side`** is `"new"` (the post-image: added and context lines) or `"old"`
  (the pre-image: a deleted line).
- **`state`** on an MR is `"open"`, `"draft"`, `"merged"`, or `"closed"`.
- **`verdict`** is `"approve"`, `"request-changes"`, or `"comment"`.
- **A comment's `state`** is `"published"` (on the forge) or `"pending"` (local,
  not yet submitted); its `origin` is `"remote"` or `"local"`.
- Optional fields are omitted when absent, not emitted as `null`.

## `show <mr> --json` — one MR's review state

The detail view an editor renders. It folds the pending draft into the remote
threads, so what you get is one merged picture.

```json
{
  "schema": 1,
  "mr": {
    "id": "123",
    "display": "#123",
    "state": "open",
    "draft": false,
    "title": "Fix the lock ordering",
    "author": "alice",
    "base": "main",
    "source": "fix-lock",
    "head_sha": "9f8e7d6c…",
    "updated_at": "2026-07-01T12:00:00Z",
    "labels": ["bug"],
    "web_url": "https://github.com/o/r/pull/123"
  },
  "snapshot": { "base_sha": "aaaa…", "head_sha": "9f8e7d6c…" },
  "neighbors": {
    "position": 1,
    "prev_mr": "122",
    "next_mr": "124",
    "nodes": ["121", "122", "123", "124"]
  },
  "commits": [ { "sha": "9f8e7d6c…", "subject": "Fix the lock ordering" } ],
  "files": [ { "path": "src/x.c", "old_path": "src/old.c", "status": "R" } ],
  "threads": [
    {
      "id": "remote:9987",
      "origin": "remote",
      "resolved": false,
      "outdated": true,
      "placement": {
        "kind": "line",
        "path": "src/x.c",
        "side": "new",
        "line": 42,
        "commit": "1111…"
      },
      "comments": [
        {
          "id": "remote:5",
          "author": "bob",
          "origin": "remote",
          "body": "This lock is taken twice.",
          "created_at": "2026-07-01T12:30:00Z",
          "state": "published"
        }
      ]
    },
    {
      "id": "local:1",
      "origin": "local",
      "resolved": false,
      "outdated": false,
      "placement": { "kind": "line", "path": "src/x.c", "side": "new", "line": 50, "commit": "9f8e7d6c…" },
      "comments": [
        { "id": "local:1", "author": "@me", "origin": "local", "body": "Agreed.", "state": "pending" }
      ]
    }
  ],
  "draft": { "verdict": "request-changes", "summary": "A few blockers.", "pending": 2 }
}
```

### Fields

| Field | Meaning |
|---|---|
| `mr` | MR metadata (see the table below). |
| `snapshot.base_sha` / `head_sha` | The two SHAs to diff. Render your diff against these. |
| `neighbors` | The MR's place in its stack. `nodes` is the chain bottom→top; `position` is this MR's index; `prev_mr`/`next_mr` are the down/up neighbours (omitted at the ends). |
| `commits` | Commits in `base..head`, oldest first — `{ sha, subject }`. |
| `files` | Files the MR touched — `{ path, old_path?, status }`; `status` is git's letter (`A`/`M`/`D`/`R`/`C`), `old_path` present on a rename/copy. |
| `threads` | Every discussion (remote + pending), merged. See below. |
| `draft` | The pending review: `verdict?`, `summary?`, and `pending` (a count of pending actions plus a verdict/summary if set). |

`mr` object: `id`, `display`, `state`, `draft` (bool), `title`, `author`, `base`
(target branch), `source` (source branch), `head_sha?`, `updated_at`, `labels`,
`web_url`.

### Threads

A thread is `{ id, origin, resolved, outdated, placement, comments }`.

- A **remote** thread's pending replies appear appended to its `comments` with
  `origin: "local"`, `state: "pending"`. A pending resolve is reflected in
  `resolved`.
- A **local** thread (a new pending comment) has a `local:` id and a single
  pending comment.

`placement` is one of:

| `kind` | Fields |
|---|---|
| `"line"` | `path`, `side`, `line`, plus optional `old_path`, `start_line` (multi-line start), `commit` (the reviewed SHA). |
| `"file"` | `path`, optional `commit`. A whole-file comment, no line. |
| `"mr"` | none. An MR-level conversation comment. |

### Filters

`--outdated`, `--resolved`, `--unread`, and `--file <path>` narrow `threads`
(AND-combined). `--unread` keeps threads whose last comment is remote (someone
else spoke last).

## `show --json` — the inbox

With no MR, `show` lists every fetched MR. Each item is the `mr` object flattened
at the top level, plus a `review` block:

```json
{
  "schema": 1,
  "items": [
    {
      "id": "123", "display": "#123", "state": "open", "draft": false,
      "title": "Fix the lock ordering", "author": "alice",
      "base": "main", "source": "fix-lock", "head_sha": "9f8e…",
      "updated_at": "2026-07-01T12:00:00Z", "labels": ["bug"],
      "web_url": "https://github.com/o/r/pull/123",
      "review": { "pending": 2, "outdated": true, "reviewed_sha": "1111…" }
    }
  ]
}
```

`review.pending` is the number of unsubmitted actions (+verdict/summary);
`review.reviewed_sha` is the snapshot your pending comments were written against;
`review.outdated` is true when that differs from the MR's current head.

Items are sorted by `updated_at`, newest first.

## `diff <mr> --json` — diff coordinates

```json
{
  "schema": 1,
  "mr": "123",
  "range": "aaaa…..9f8e…",
  "base_sha": "aaaa…",
  "head_sha": "9f8e…",
  "commits": [ { "sha": "…", "subject": "…" } ],
  "files": [ { "path": "src/x.c", "status": "M" } ]
}
```

The tool does not render diffs — this gives the coordinates (SHAs, commits,
touched files) so the editor renders its own diff against `base_sha`/`head_sha`
and knows where a comment may anchor. `--range` changes what `range`/`commits`/
`files` describe.

## `draft <mr> --json` — the pending draft

The raw pending review for one MR — useful to a front-end that wants to show or
diff what will be submitted.

```json
{
  "schema": 1,
  "verdict": "request-changes",
  "summary": "A few blockers.",
  "actions": [
    { "action": "comment", "id": "local:1",
      "placement": { "kind": "line", "path": "src/x.c", "side": "new", "line": 50, "commit": "9f8e…" },
      "body": "Agreed." },
    { "action": "reply", "id": "local:2", "thread": "9987", "body": "Done." },
    { "action": "resolve", "thread": "9987", "resolved": true }
  ],
  "seq": 2
}
```

`actions` is a tagged union on `action`: `"comment"` (a new thread — its
`placement` is the same shape as above), `"reply"` (to a remote `thread`, given
here without the `remote:` prefix), and `"resolve"` (`resolved` bool). `seq` is
the internal local-id counter.

## The write path (for reference)

A front-end mutates the draft by invoking the CLI, with the body on stdin:

| Intent | Command |
|---|---|
| New line comment | `wits review comment <mr> --line PATH:LINE[:side] [--start-line N]` |
| New file comment | `wits review comment <mr> --file PATH` |
| New conversation comment | `wits review comment <mr> --mr-level` |
| Reply | `wits review comment <mr> --reply <thread-id>` |
| Edit a pending body | `wits review comment <mr> --edit <local-id>` |
| Drop a pending action | `wits review drop <mr> <local-id>` |
| Set a verdict | `wits review verdict <mr> {approve\|request-changes\|comment}` |
| Resolve / unresolve | `wits review resolve\|unresolve <mr> <thread-id>` |

Thread ids accept the `remote:` prefix from `show` output or the bare forge id.
After a mutation, re-run `show`/`draft` to get the updated state — ids are stable
within a draft (a dropped id is never reused) until `submit` clears it.
