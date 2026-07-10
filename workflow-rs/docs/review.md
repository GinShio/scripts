# `wits review`

Review merge requests locally, across forges, from your editor or the terminal.
`wits review` is the mirror image of [`wits stack`](stack.md): where `stack` owns
the *existence and structure* of a set of MRs, `review` owns their *review
content* — the diff you read, the threads you leave, the verdict you render. It
never touches the code or the branches; it fetches an MR's objects, lets you
build a review in a local file, and submits it as one batch.

> Terminology: GitHub says *pull request*, GitLab *merge request*. This tool says
> **MR** everywhere; on a GitHub repo the output just says "PR".

Two ideas shape everything:

1. **Any MR, not just yours.** You address an MR by number; the tool asks the
   forge what it is and fetches its objects. No local branch, no authorship
   required.
2. **You author by editing a file, not by running commands.** There are no
   `comment`/`verdict`/`resolve` verbs. You edit a plain `local.json` draft (by
   hand or through an editor), and two verbs move data over the network: `fetch`
   reads, `submit` writes.

The rationale is in [`review/design.md`](review/design.md); the exact JSON shapes
are in [`review/json.md`](review/json.md); the on-disk store and how to move it
between machines are in [`review/store.md`](review/store.md). This guide is the
getting-things-done level.

## The mental model

Each MR is described by three local files (details in [store.md](review/store.md)):

| File | Holds | Written by |
|---|---|---|
| `info.json` | the MR's metadata and diff state (the inbox row) | `fetch` |
| `comments.json` | the forge's discussion (a refetchable cache) | `fetch` |
| `local.json` | **your** unsubmitted verdict + comments | **you** (edit it) |

```
fetch  ─────────►  info.json + comments.json        ◄── you edit ──  local.json
(network read)     (+ pinned objects)                                    │
                                                                         ▼
                                                                      submit
                                                                  (network write)
```

- `fetch` pulls an MR's metadata, objects (pinned so a later force-push can't
  lose them), and discussion.
- You edit `local.json` to record your review — nothing reaches the forge.
- `submit` merges and posts the draft as one batched review, clears it, and
  re-fetches, so your just-posted comments come back as ordinary remote threads.

## One-time setup

### A token for your forge

`fetch` and `submit` need a forge API token, configured exactly as for `stack`:

| Where | Example |
|---|---|
| git config, per host | `git config workflow.platform.github.com.token ghp_xxx` |
| git config, blanket | `git config workflow.platform.token ghp_xxx` |
| environment | `export GITHUB_TOKEN=ghp_xxx` (or `GITLAB_TOKEN`) |

The forge (GitHub or GitLab in this version) is detected from the **upstream**
remote's URL, or `origin` when there is no upstream. A self-hosted instance:

```sh
git config workflow.platform.git.acme.com.service gitlab
git config workflow.platform.git.acme.com.api-url https://git.acme.com/api/v4
```

Only `fetch` and `submit` need a token; the read verbs (`show`, `diff`, `draft`)
and `checkout`/`prune` need just a remote to identify the repo.

## The commands

Seven verbs; only `fetch` and `submit` touch the network.

| Verb | Network | What it does |
|---|---|---|
| `fetch [mr] [--feed name]` | read | Pull one MR (full), a feed (light), or every feed (bare). |
| `show [mr] [filters] [--json]` | — | The inbox, or one MR's merged review view. |
| `diff <mr> [--range r] [--patch\|--json]` | — | A diff's commits/files/coordinates. |
| `draft <mr> [--json]` | — | The pending `local.json`, echoed and normalized. |
| `submit [mr] [--stack\|--all]` | write | Flush the draft(s) as batched reviews. |
| `checkout [mr] [--next\|--prev] [--in-place\|--worktree DIR]` | — | Materialize the code to build/test. |
| `prune [--older-than DAYS\|DATE]` | — | Drop terminal/dormant MRs. |

### Fetching

```sh
wits review fetch 123                       # one MR, by number — a full pull
wits review fetch https://github.com/o/r/pull/123   # …or by URL
wits review fetch --feed mine               # one feed's MRs (light: metadata only)
wits review fetch                           # every configured feed (light)
```

Fetching one MR is a **full** pull (objects, discussion, derived commit/file
lists). Fetching a feed is **light** — only the inbox metadata for each matching
MR, leaving the per-MR pull to `fetch <mr>`. That is what lets a feed scale to a
repo with thousands of open MRs.

### Feeds — an RSS-style subscription

A feed is a named, server-side filter. Feeds live in one global TOML file,
`$XDG_CONFIG_HOME/wits/review.toml` (or `$WITS_REVIEW_CONFIG`), with a section
per repo keyed by its `host/owner/repo` identity:

```toml
[repo."github.com/mesa/mesa"]
feed.mine   = { reviewer = "@me", state = "open+draft" }
feed.vulkan = { labels = ["vulkan"], exclude-labels = ["wip"], limit = 40 }
```

Every key is optional; see the [configuration reference](#configuration-reference)
for the full table. Filters are pushed down to the forge and paginated
server-side — never "fetch everything then filter". A repo with no section simply
has no feeds; a token alone still lets you `fetch <number>` any single MR.

### Reading

```sh
wits review show                 # the inbox: every fetched MR, newest first
wits review show 123             # one MR: metadata, snapshot, commits, files, threads
wits review show 123 --json      # …as the stable editor payload
```

The detail view folds your pending draft into the remote threads: your new
comments appear as local threads, your replies attach to their threads, your
resolutions flip the flag. For a large MR, **filter** instead of paginate:

| Filter | Keeps threads that… |
|---|---|
| `--outdated` | are anchored to a line no longer in the current diff |
| `--resolved` | are resolved |
| `--unread` | have someone else's comment last (likely awaiting your reply) |
| `--file PATH` | are anchored in `PATH` |

Diff coordinates (the tool does not render diffs — your editor and `git` do):

```sh
wits review diff 123                 # commits + changed files of base..head
wits review diff 123 --range a1b2..c3d4
wits review diff 123 --patch         # the textual patch, via git (terminal/debug)
wits review diff 123 --json          # coordinates for an editor
```

`--range` is `all` (the whole `base..head`, default), a git range `A..B`, or a
single revision.

## Authoring a review — edit `local.json`

There are **no authoring commands**. To review, you edit the MR's `local.json`
(an editor extension writes it for you; a human can too). Its full schema is in
[json.md](review/json.md#localjson---the-write-contract); the shape:

```json
{
  "schema": 1,
  "verdict": "request-changes",
  "summary": "A few blockers below.",
  "actions": [
    { "action": "comment", "file": "src/x.c", "line": 42, "body": "Off-by-one." },
    { "action": "comment", "file": "src/x.c", "line": 40, "start_line": 38, "side": "old", "body": "Was this intended?" },
    { "action": "comment", "file": "src/x.c", "body": "This file wants a header." },
    { "action": "comment", "body": "Overall close." },
    { "action": "reply", "thread": "9987", "body": "Done." },
    { "action": "resolve", "thread": "9987", "resolved": true }
  ]
}
```

Rules, all inferred so the file is pleasant to hand-write:

- **`verdict`** (optional): `approve`, `request-changes`, or `comment`.
- **`summary`** (optional): the review's overall body.
- **A `comment` action's placement** is inferred: `file`+`line` → a line comment;
  `file` alone → a file-level comment; neither → an MR-level conversation comment.
  `side` (`new`/`old`, default `new`) and `start_line` (a multi-line start) are
  optional.
- **`reply`** targets a thread id (the bare forge id, or the `remote:` form
  `show` prints).
- **`resolve`** sets a thread's resolved state (GitLab in v1).

Preview what's recorded any time, without touching the forge:

```sh
wits review draft 123           # human
wits review draft 123 --json    # machine
```

## Submitting

```sh
wits review submit 123          # one MR
wits review submit 123 --stack  # every drafted MR in 123's stack
wits review submit --all        # every MR that has a pending draft
```

On submit, the draft is **merged and de-duplicated** (an accidentally repeated
comment is dropped; repeated resolutions of one thread collapse to the last),
then posted. The verdict, summary, and line/file comments go up as **one** call
where the platform allows it (GitHub's review API; GitLab's draft notes bulk
published, plus a separate `approve` for an approving verdict) — a single
notification. MR-level comments, replies, and resolves are separate calls.

Reconciliation is **per action**: whatever lands is cleared from `local.json`,
whatever fails stays for a retry, and only a fully-flushed draft triggers a
re-fetch. Preview exactly what would be posted with `-n`:

```sh
wits review submit 123 -n
```

## Reviewing the code itself: `checkout`

To build, run, or fuzz an MR, materialize its code:

```sh
wits review checkout 123               # into a worktree (leaves your tree alone)
wits review checkout 123 --worktree /tmp/mr123
wits review checkout 123 --in-place    # in the current tree (moves HEAD)
wits review checkout --next            # the MR one step up the stack
wits review checkout --prev            # one step down
```

The default is a **worktree** at a sibling path (`../<repo>.review/mr-<id>`),
which lets you review several MRs at once. `--in-place` checks the snapshot out
in your current tree; because that moves `HEAD`, it **refuses a dirty tree**.
`--next`/`--prev` walk the stack from the last checkout — the shape is
reconstructed from the fetched MRs' base/source branches, so it works for
anyone's stack.

## Housekeeping: `prune`

```sh
wits review prune                    # merged/closed MRs
wits review prune --older-than 30    # …and any not refreshed in 30 days
wits review prune --older-than 2026-06-01   # …or last refreshed before a date
```

`prune` drops the store directory and snapshot pins (`refs/wits/review/*`) of
terminal MRs, letting git collect the objects. `--older-than` also catches
dormant MRs, given a **day count** or an **ISO-8601 date**. It is idempotent and
a no-op when nothing is stale.

## Outdating

A review is pinned to the snapshot you fetched. Comments submit against it, so
when the branch has moved on the forge shows them as *outdated* rather than
re-basing them onto code they were never about. `show --outdated` surfaces
threads whose anchored line has left the current diff. The reviewed objects are
held alive by `refs/wits/review/*` even after the author force-pushes, so an
outdated comment can still be submitted.

## Configuration reference

### Tokens and forge detection (git config, shared with `stack`)

| Setting | Key / variable | Notes |
|---|---|---|
| Token (per host) | `workflow.platform.<host>.token` | Most specific. |
| Token (per service) | `workflow.platform.<service>.token` | `<service>` ∈ github, gitlab. |
| Token (blanket) | `workflow.platform.token` | Last config fallback. |
| Token (env) | `GITHUB_TOKEN`, `GITLAB_TOKEN` | Used when no config key matches. |
| Service override | `workflow.platform.<host>.service` | Name a self-hosted host's type. |
| API base override | `workflow.platform.<host>.api-url` | Self-hosted / enterprise. |

### Feeds (`review.toml`)

The file is a single global TOML at `$WITS_REVIEW_CONFIG`, else
`$XDG_CONFIG_HOME/wits/review.toml`, else `$HOME/.config/wits/review.toml`.
Each repo is a `[repo."<host>/<owner>/<repo>"]` table; each feed is a
`feed.<name>` inline table of these keys:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `state` | string | `open+draft` | Which lifecycle states to pull: `open+draft`, `open`, or `draft`. Merged/closed are never fetched. |
| `labels` | list | `[]` | Only MRs carrying **all** of these labels. |
| `exclude-labels` | list | `[]` | Drop MRs carrying any of these labels. |
| `author` | string | — | Only MRs opened by this user. `@me` is you. |
| `assignee` | string | — | Only MRs assigned to this user. `@me` is you. |
| `reviewer` | string | — | Only MRs with this user requested as reviewer. `@me` is you. |
| `search` | string | — | A raw platform search string (the full-text escape hatch). |
| `limit` | integer | `30` | Cap on how many MRs to pull (most recently updated first). |

> Fields are combined with AND; a repo query only pulls MRs matching all of them.
> Multiple `labels` are AND-ed on both GitHub and GitLab (the platforms' own
> behaviour for a single list query) — use separate feeds for either-or.

### Store location (state, distinct from config)

| Rung | Path |
|---|---|
| 1 | `$WITS_REVIEW_DIR` |
| 2 | `$XDG_STATE_HOME/wits/review` |
| 3 | `$GIT_DIR/wits/review` (default, per-clone) |

Per-run choices (`--range`, `--stack`, `--all`, `-n`) are flags, not config.

## Version scope and limitations

Bounded on purpose, and honest about it:

| Area | v1 behaviour |
|---|---|
| Forges | GitHub and GitLab. Gitea/Forgejo/Codeberg have the trait seam but no backend. |
| Thread resolve | GitLab only; GitHub's is GraphQL-only and deferred (the tool speaks REST). A `resolve` action submitted to a GitHub MR reports the gap. |
| `request-changes` on GitLab | No native equivalent; maps to "post the review and do not approve". |
| Editing/deleting a **published** comment | Not supported; you edit only your pending `local.json`. |
| Outdated draft | If you re-`fetch` after drafting and the head moved, `submit` warns and posts against the currently-held snapshot. Submit before a refresh for the smooth path. |
| Feeds | Pull the most recently updated MRs up to `limit`; an incremental "since last sync" cursor is future work. |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `no 'origin' or 'upstream' remote…` | `review` keys off the target remote; add one. |
| `no API token for …` | Set `workflow.platform.<host>.token` or `*_TOKEN` (fetch/submit only). |
| `MR N isn't in the store yet` | Run `wits review fetch N` first — read verbs use the local files. |
| `no feed 'x'` | The repo has no `feed.x` under its `[repo."…"]` section in `review.toml`. |
| `no feeds configured for …` | Bare `fetch` needs at least one feed; add one, or name an MR. |
| `working tree has uncommitted changes` (checkout) | In-place checkout moves HEAD; commit/stash first, or use a worktree. |
| `some actions did not submit` | A per-action failure; the failed ones stayed in `local.json` — fix and re-`submit`. |

## Invocation forms

Like every `wits` tool, `review` has a direct form via symlink — `wits-review` —
created by `meson install` (see the top-level [README](../README.md)).
