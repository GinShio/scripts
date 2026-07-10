# `wits review` — the JSON contract

This is the interface between `wits review` and an editor (or any front-end).

- **Read** through `--json`: `show`, `diff`, and `draft` emit versioned JSON on
  stdout.
- **Write** by editing `local.json` — the draft file whose schema is defined
  [below](#localjson---the-write-contract). A front-end writes this file; there
  are no authoring commands.

Every payload carries an integer `schema` (currently `1`). A reader that meets a
schema it doesn't know should refuse rather than guess.

## Shared conventions

| Concept | Values | Meaning |
|---|---|---|
| Id prefix | `remote:<forge-id>` / `local:<n>` | Whether the forge owns the object, or it's a pending draft item. |
| `side` | `new` / `old` | Post-image (added/context) / pre-image (deleted line). |
| `state` (MR) | `open` / `draft` / `merged` / `closed` | The MR's lifecycle, draft folded in. |
| `verdict` | `approve` / `request-changes` / `comment` | The reviewer's disposition. |
| `origin` (comment) | `remote` / `local` | On the forge / pending in your draft. |
| `state` (comment) | `published` / `pending` | On the forge / not yet submitted. |

Optional fields are omitted when absent, never emitted as `null`.

---

## `show <mr> --json` — one MR's review state

The detail view, with the pending draft folded into the remote discussion.

```json
{
  "schema": 1,
  "mr": { "...": "MrInfo, see below" },
  "snapshot": { "base_sha": "aaaa…", "head_sha": "9f8e…" },
  "neighbors": { "position": 1, "prev_mr": "122", "next_mr": "124",
                 "nodes": ["121","122","123","124"] },
  "commits": [ { "sha": "9f8e…", "subject": "Fix the lock ordering" } ],
  "files": [ { "path": "src/x.c", "old_path": "src/old.c", "status": "R" } ],
  "threads": [ { "...": "Thread, see below" } ],
  "draft": { "verdict": "request-changes", "summary": "…", "pending": 2 }
}
```

### Top-level fields

| Field | Type | Meaning |
|---|---|---|
| `schema` | int | Payload version. |
| `mr` | object | MR metadata (table below). |
| `snapshot.base_sha` | string | The base SHA of the reviewed diff. |
| `snapshot.head_sha` | string | The head SHA under review. **Render your diff between these two.** |
| `neighbors` | object | This MR's place in its stack (table below). |
| `commits` | array | Commits in `base..head`, oldest first: `{ sha, subject }`. |
| `files` | array | Files the MR touched: `{ path, old_path?, status }`. `status` is git's letter (`A`/`M`/`D`/`R`/`C`); `old_path` present on a rename/copy. |
| `threads` | array | Every discussion, remote + pending, merged (table below). |
| `draft` | object | The pending review: `verdict?`, `summary?`, and `pending` — a count of pending actions (plus one if a verdict/summary is set). |

### `mr` object

| Field | Type | Meaning |
|---|---|---|
| `id` | string | The MR number (the address you pass to commands). |
| `display` | string | Human form (`#123` / `!123`). |
| `state` | string | `open`/`draft`/`merged`/`closed`. |
| `draft` | bool | Whether the MR is a draft/WIP. |
| `title`, `author` | string | As on the forge. |
| `base` | string | Target branch (what it merges into). |
| `source` | string | Source branch (used to link a stack). |
| `head_sha` | string? | Current head; omitted if unknown. |
| `updated_at` | string | ISO-8601 last-update time. |
| `labels` | array | Label names. |
| `web_url` | string | The MR's forge URL. |

### `neighbors` object

| Field | Type | Meaning |
|---|---|---|
| `nodes` | array | The stack's MR ids, bottom→top. |
| `position` | int | This MR's index in `nodes`. |
| `prev_mr` | string? | The MR one step down (omitted at the bottom). |
| `next_mr` | string? | The MR one step up (omitted at the top). |

### `Thread` object

| Field | Type | Meaning |
|---|---|---|
| `id` | string | `remote:<id>` for a forge thread, `local:<n>` for a pending one. |
| `origin` | string | `remote` / `local`. |
| `resolved` | bool | Resolved on the forge (reflects a pending resolve too). |
| `outdated` | bool | The anchored line has left the current diff. |
| `placement` | object | Where the thread sits (table below). |
| `comments` | array | The thread's comments; a pending reply is appended with `origin: local`, `state: pending`. |

`placement` is one of:

| `kind` | Fields | Meaning |
|---|---|---|
| `line` | `path`, `side`, `line`, `old_path?`, `start_line?`, `commit?` | A code line. `start_line` marks a multi-line span; `commit` is the reviewed SHA. |
| `file` | `path`, `commit?` | A whole changed file, no line. |
| `mr` | — | The MR conversation, no code anchor. |

`Comment` object: `id`, `author`, `origin`, `body`, `created_at?`, `state`.

### Filters

`--outdated`, `--resolved`, `--unread`, `--file <path>` narrow `threads`
(AND-combined). `--unread` keeps threads whose last comment is `remote`.

---

## `show --json` — the inbox

With no MR, an array of MRs. Each item is the `mr` object flattened at the top
level, plus a `review` block.

```json
{
  "schema": 1,
  "items": [
    { "id": "123", "display": "#123", "state": "open", "draft": false,
      "title": "Fix the lock ordering", "author": "alice",
      "base": "main", "source": "fix-lock", "head_sha": "9f8e…",
      "updated_at": "2026-07-01T12:00:00Z", "labels": ["bug"],
      "web_url": "https://…/123",
      "review": { "pending": 2 } }
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| (mr fields) | — | Same as the `mr` object above, flattened. |
| `review.pending` | int | Count of unsubmitted actions in this MR's draft (plus one for a set verdict/summary). |

Items are sorted by `updated_at`, newest first.

---

## `diff <mr> --json` — diff coordinates

```json
{ "schema": 1, "mr": "123", "range": "aaaa…..9f8e…",
  "base_sha": "aaaa…", "head_sha": "9f8e…",
  "commits": [ { "sha": "…", "subject": "…" } ],
  "files": [ { "path": "src/x.c", "status": "M" } ] }
```

| Field | Type | Meaning |
|---|---|---|
| `mr` | string | The MR number. |
| `range` | string | The resolved range (`all` expands to `base..head`). |
| `base_sha`, `head_sha` | string | The two SHAs to diff against. |
| `commits` | array | Commits in the range: `{ sha, subject }`. |
| `files` | array | Files touched in the range: `{ path, old_path?, status }`. |

The tool renders no diff — this gives the coordinates so the editor renders its
own against `base_sha`/`head_sha`.

---

## `draft <mr> --json` — the pending draft

`draft --json` prints the MR's `local.json` verbatim (see the write contract).

---

## `local.json` — the write contract

This is the file a front-end (or a human) edits to author a review. It is the
one place `wits review` reads authored intent from, so its shape is a public,
versioned contract.

```json
{
  "schema": 1,
  "verdict": "request-changes",
  "summary": "A few blockers below.",
  "actions": [
    { "action": "comment", "file": "src/x.c", "line": 42, "body": "Off-by-one." },
    { "action": "comment", "file": "src/x.c", "line": 42, "start_line": 40, "side": "old", "body": "…" },
    { "action": "comment", "file": "src/x.c", "body": "File-level note." },
    { "action": "comment", "body": "MR-level conversation note." },
    { "action": "reply", "thread": "9987", "body": "Done." },
    { "action": "resolve", "thread": "9987", "resolved": true }
  ]
}
```

### Top level

| Field | Type | Required | Meaning |
|---|---|---|---|
| `schema` | int | yes | Contract version (`1`). |
| `verdict` | string | no | `approve` / `request-changes` / `comment`. Omit for comments-only. |
| `summary` | string | no | The review's overall body. |
| `actions` | array | no | The ordered list of things to post. |

### `actions[]` — tagged on `action`

**`comment`** — a new thread. Placement is inferred from which fields are present:

| Fields present | Placement |
|---|---|
| `file` + `line` | a line comment |
| `file` only | a file-level comment |
| neither | an MR-level conversation comment |

| Field | Type | Required | Meaning |
|---|---|---|---|
| `file` | string | no | Path of a changed file. |
| `line` | int | no | Line number on `side`. |
| `side` | string | no | `new` (default) or `old`. |
| `start_line` | int | no | First line of a multi-line span (with `line` as the end). |
| `body` | string | yes | The comment text. |

**`reply`** — add to an existing thread.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `thread` | string | yes | The thread id (bare forge id, or `remote:` form). |
| `body` | string | yes | The reply text. |

**`resolve`** — set a thread's resolved state (GitLab in v1).

| Field | Type | Required | Meaning |
|---|---|---|---|
| `thread` | string | yes | The thread id. |
| `resolved` | bool | yes | `true` to resolve, `false` to unresolve. |

### How `submit` treats it

- **Merge + de-duplicate:** exact-duplicate actions are dropped; repeated
  `resolve` of one thread collapses to the last stated value.
- **Batching:** `verdict` + `summary` + all line/file `comment`s post as one
  review; MR-level comments, replies, and resolves are separate calls.
- **Anchoring:** line/file comments submit against the reviewed snapshot's head
  (from `info.json`). Editing after a re-fetch that moved the head triggers a
  warning; submit before refreshing for exact anchoring.
- **After submit:** landed actions are removed and `local.json` is deleted once
  empty; failed actions stay for a retry.
