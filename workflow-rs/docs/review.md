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
| `diff <mr> [--range r\|--snapshot sha] [--patch\|--json]` | — | A diff's commits/files/coordinates. |
| `draft <mr> [FILE\|-] [--json]` | — | Show the pending draft, or append a batch of actions to it. |
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
| `--unresolved` | are not yet resolved |
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
single revision. `--snapshot <sha>` browses a **historical** snapshot instead
(see below).

### Snapshots vs. ranges

These are two different things, kept apart on purpose:

- A **snapshot** is a review point you fetched: its base/start/head SHAs, pinned
  so the objects survive a later force-push. Every `fetch` that sees a new head
  records one, so `info.json` accumulates a history. `show --json` lists them
  under `snapshots`.
- A **range** is a throwaway diff query (`base..head`, `A..B`, a commit) — never
  stored.

To review the code as of an older snapshot (browse "outdated context"):

```sh
wits review show 123 --json          # the "snapshots" array lists head SHAs + times
wits review diff 123 --snapshot 1a2b3c   # that snapshot's base..head (prefix ok)
```

Because every snapshot's objects are pinned, this works even after the author
has force-pushed past them.

## Authoring a review — edit `local.json`

There are **no authoring commands**. You produce the content; the tool writes it
into `local.json`. Two equivalent ways:

- **Pipe a batch to the tool** (the path an editor extension uses):
  ```sh
  wits review draft 123 -   # read a JSON batch of actions from stdin; a file path also works
  ```
  This appends the batch's actions to the draft (setting the verdict/summary if
  the batch carries them), and validates as it writes.
- **Edit `local.json` directly** (the plain-text path for a human). It is the
  same file; both are equivalent.

To edit or remove a *queued* action, edit `local.json`. Its full schema is in
[json.md](review/json.md#localjson---the-write-contract); the shape:

```json
{
  "schema": 1,
  "verdict": "request-changes",
  "summary": "A few blockers below.",
  "actions": [
    { "action": "comment", "file": "src/x.c", "line": 42, "body": "Off-by-one.", "commit": "a1b2c3d4" },
    { "action": "comment", "file": "src/x.c", "line": 40, "start_line": 38, "side": "old", "start_side": "old", "body": "Was this intended?", "commit": "a1b2c3d4" },
    { "action": "comment", "file": "src/x.c", "body": "This file wants a header.", "commit": "a1b2c3d4" },
    { "action": "comment", "body": "Overall close." },
    { "action": "reply", "thread": "9987", "body": "Done." },
    { "action": "resolve", "thread": "9987", "resolved": true }
  ]
}
```

Rules, all inferred so the file is pleasant to hand-write:

- **`verdict`** (optional): `approve`, `request-changes`, or `comment`.
- **`summary`** (optional): the review's overall body.
- **`commit`** on a comment (optional): the snapshot head SHA the comment's line
  anchors were written against. `draft <mr> -` stamps it automatically at ingest;
  a hand-editor may set it. `submit` resolves it to the snapshot's full version
  (`{base, start, head}`) and anchors the comment to it — the forge may mark it
  outdated if the head has moved. Different comments in one draft can target
  different snapshots (cross-snapshot drafting — fully per-comment on GitLab; on
  GitHub the whole review anchors to one review-level commit). When unset,
  `submit` falls back to the current snapshot's head.
- **A `comment` action's placement** is inferred: `file`+`line` → a line comment;
  `file` alone → a file-level comment; neither → an MR-level conversation comment.
  `side` (`new`/`old`, default `new`) and `start_line` (a multi-line start) are
  optional; `start_side` (defaults to `side`) marks a span that starts on one side
  and ends on the other (e.g. a deleted line through to an added one).
- **`reply`** targets a thread id (the bare forge id, or the `remote:` form
  `show` prints).
- **`resolve`** sets a thread's resolved state (supported on both forges).

Preview what's recorded any time, without touching the forge:

```sh
wits review draft 123           # human
wits review draft 123 --json    # machine (echoes local.json)
```

### Referencing another line or file

A comment body may reference another location with a `[[…]]` token, which
`submit` expands into a forge permalink (so it renders as a link, while your
local body stays plain and portable):

| Token | Refers to |
|---|---|
| `[[src/y.c:20]]` | line 20 of `src/y.c` (path is repo-relative) |
| `[[src/y.c:20-30]]` | lines 20–30 |
| `[[src/y.c]]` | the whole file, no line |
| `[[src/y.c:20@main]]` | line 20 as of another commit/branch/tag (`@ref`) |

The reference resolves against the **reviewed snapshot's head** by default; the
optional `@ref` pins any other commit, branch, or tag. Example:

```json
{ "action": "comment", "file": "src/x.c", "line": 42,
  "body": "Same bug as [[src/y.c:20]] — factor them together." }
```

## Submitting

```sh
wits review submit 123          # one MR
wits review submit 123 --stack  # every drafted MR in 123's stack
wits review submit --all        # every MR that has a pending draft
```

On submit, the draft is **merged and de-duplicated** (an accidentally repeated
comment is dropped; repeated resolutions of one thread collapse to the last),
then handed to the forge as one review. Each platform folds as much as its native
batch allows into **one notification**:

- **GitLab** — comments (line/file/conversation), replies, the summary, and a
  `request-changes`/`comment` reviewer state all ride a single `bulk_publish`. An
  `approve` verdict (a real approval, which `bulk_publish` can't record) and a
  bare thread resolve are separate (quiet) calls.
- **GitHub** — the verdict, summary, line/file comments, **and replies** are one
  review (replies join the pending review by id, exactly as the web UI batches
  them), so they share one notification. Only a conversation (MR-level) comment
  is a separate notification — that one *is* a GitHub API limit. Resolves are
  separate calls but don't notify.

`submit` reports how many notifications it actually produced, so there are no
surprises. Reconciliation is **per action**: whatever lands is cleared from
`local.json`, whatever fails stays for a retry, and only a fully-flushed draft
triggers a re-fetch. Preview exactly what would be posted with `-n`:

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
when the branch has moved they are shown as *outdated* rather than re-based onto
code they were never about. **`wits review` computes outdating itself**, locally
and identically for every forge: a thread is outdated when its anchored line
falls inside a region the file changed between the commit the comment was written
on and the current head — read from the objects `fetch` pinned, no network, no
reliance on a forge flag. `show --outdated` surfaces exactly those threads. The
reviewed objects are held alive by `refs/wits/review/*` even after the author
force-pushes, so an outdated comment can still be submitted.

## Configuration reference

Three surfaces: **environment variables** (locations and tokens),
**git config** (forge identity and tokens, shared with `stack`), and the feed
**`review.toml`**. Every key is listed below with what it does.

### Environment variables

- **`WITS_REVIEW_DIR`** — Absolute path to the store root, overriding the default
  location. Point it at synced storage to share your drafts across machines. See
  *Store location* below for how it fits the ladder.
- **`WITS_REVIEW_CONFIG`** — Absolute path to the feed config file, overriding the
  default `review.toml` location. Handy to keep one config outside `$HOME`.
- **`XDG_STATE_HOME`** — When set (and `WITS_REVIEW_DIR` isn't), the store lives at
  `$XDG_STATE_HOME/wits/review`. This is *state*, not config.
- **`XDG_CONFIG_HOME`** — When set (and `WITS_REVIEW_CONFIG` isn't), the feed file
  is `$XDG_CONFIG_HOME/wits/review.toml`. This is *config*, not state.
- **`GITHUB_TOKEN` / `GITLAB_TOKEN`** — The forge API token, used by `fetch`/
  `submit` when no git-config token key matches. The one that applies is chosen
  by the detected service.
- **`HOME`** — Falls back to `$HOME/.config/wits/review.toml` for the feed file
  when neither override nor `XDG_CONFIG_HOME` is set.

### Git config (under `workflow.platform.*`, shared with `stack`)

Token resolution tries these in order, most specific first, then the env var:

- **`workflow.platform.<host>.token`** — Token for one host (e.g.
  `github.com`). The most precise, and what you usually set.
- **`workflow.platform.<service>.token`** — Token for a whole service
  (`<service>` ∈ `github`, `gitlab`), when several hosts share a service.
- **`workflow.platform.token`** — A blanket token, the last config fallback
  before the environment.
- **`workflow.platform.<host>.service`** — Declares a self-hosted host's type
  (`github` / `gitlab`) when the hostname doesn't reveal it.
- **`workflow.platform.<host>.api-url`** — The API base for a self-hosted or
  enterprise instance (e.g. `https://git.acme.com/api/v4`).

### Feeds — `review.toml`

The file is a single global TOML, found at `$WITS_REVIEW_CONFIG`, else
`$XDG_CONFIG_HOME/wits/review.toml`, else `$HOME/.config/wits/review.toml`. Each
repo is a table `[repo."<host>/<owner>/<repo>"]`; inside it, each feed is an
inline table `feed.<name> = { … }`. The feed keys, each optional:

- **`state`** *(string, default `"open+draft"`)* — Which lifecycle states to
  pull: `"open+draft"`, `"open"`, or `"draft"`. Merged and closed MRs are never
  fetched — a review inbox is about live work.
- **`labels`** *(list, default `[]`)* — Only MRs carrying **all** of these labels.
  Multiple labels are AND-ed on both GitHub and GitLab (the platforms' own
  behaviour for a single list query); use separate feeds when you want either-or.
- **`exclude-labels`** *(list, default `[]`)* — Drop MRs carrying **any** of these
  labels — the way to filter out `wip`/bot noise.
- **`author`** *(string)* — Only MRs opened by this user. `@me` resolves to the
  authenticated user.
- **`assignee`** *(string)* — Only MRs assigned to this user. `@me` is you.
- **`reviewer`** *(string)* — Only MRs with this user requested as a reviewer.
  `@me` is you — this is the "assigned to me to review" feed.
- **`search`** *(string)* — A raw platform search string, passed straight through
  for the full-text case the faceted keys can't express.
- **`limit`** *(integer, default `30`)* — A cap on how many MRs the feed pulls,
  most-recently-updated first, so a large repo can't flood the inbox.

Different keys are combined with **AND**: a feed pulls only MRs matching all of
them. `@me` works in `author`/`assignee`/`reviewer`.

### Store location (state, distinct from config)

The store root is resolved on this ladder, first hit wins:

- **`$WITS_REVIEW_DIR`** — an explicit override, when set.
- **`$XDG_STATE_HOME/wits/review`** — when `XDG_STATE_HOME` is set.
- **`$GIT_DIR/wits/review`** — the default, per-clone (beside `.git/machete`).

Per-run choices (`--range`, `--snapshot`, `--stack`, `--all`, `-n`) are flags,
not config — they describe one invocation.

## Version scope and limitations

Bounded on purpose, and honest about it:

| Area | behaviour |
|---|---|
| Forges | GitHub (GraphQL) and GitLab (REST). Gitea/Forgejo/Codeberg have the trait seam but no review backend. |
| Thread resolve | Supported on **both** — GitHub via `resolveReviewThread`, GitLab via the discussion API. |
| `request-changes` on GitLab | **Native** (`reviewer_state: requested_changes`), targeting GitLab ≥ 19; `comment` is native too (`reviewed`). An `approve` verdict is a separate real-approval call (`POST …/approve`), because `bulk_publish`'s `approved` records only a review state, not a formal approval. |
| Editing/deleting a **published** comment | Not supported; you edit only your pending `local.json`. |
| Cross-snapshot anchoring | Per-comment on GitLab (each comment anchors to its own snapshot version); review-level on GitHub (its API takes one commit per review, so the batch anchors to one snapshot). Comments without a `commit` use the current snapshot. |
| Outdating | Computed **locally** and identically for both forges — a thread is outdated when its anchored line changed between the commit it was written on and the current head. Falls back to the forge's own flag only when that commit's objects aren't local. |
| Feeds | Return real MRs (base/head) up to a hard `limit`, most-recently-updated first; an incremental "since last sync" cursor is future work. |
| Notifications | Minimised, not promised: `submit` reports the true count. GitLab folds a whole review into one `bulk_publish`. GitHub folds the verdict, summary, line/file comments, and replies into one review; only an MR-level conversation comment is a separate notification (resolves are separate but quiet). |

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
