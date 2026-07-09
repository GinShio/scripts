# `wits review`

Review merge requests locally, across forges, from your editor or the terminal.
`wits review` is the mirror image of [`wits stack`](stack.md): where `stack` owns
the *existence and structure* of a set of MRs, `review` owns their *review
content* — the diff you read, the threads you leave, the verdict you render. It
never touches the code or the branches; it fetches an MR's objects, lets you
build up a review locally, and submits it as one batch.

> Terminology: GitHub says *pull request*, GitLab *merge request*. This tool says
> **MR** everywhere; on a GitHub repo the output just says "PR".

The two ideas that shape everything below:

- **Any MR, not just yours.** You address an MR by number; the tool asks the
  forge what it is and fetches its objects. No local branch, no `.git/machete`,
  no authorship required.
- **Only two verbs touch the network.** `fetch` reads; `submit` writes. Every
  comment, reply, edit, verdict, and resolve in between is recorded into a local
  *draft* and flushed as one batch — so a review lands as a single notification,
  not one per keystroke.

The *why* behind the design lives in [`review/design.md`](review/design.md); the
machine-readable contract an editor speaks is in
[`review/json.md`](review/json.md); the on-disk store and how to move it between
machines is in [`review/store.md`](review/store.md). This guide is the
getting-things-done level.

## The mental model

```
fetch  ─────────────►  local store  ◄─────── comment / verdict / resolve …
(network read)          (cache + draft)        (local only)
                              │
                              └───────────►  submit  ──────────►  the forge
                                             (network write)
```

- **`fetch`** pulls an MR's metadata, its objects (pinned so a later force-push
  can't lose them), and its existing review threads into a local **cache**.
- The **authoring verbs** record your intent into a local **draft** — nothing
  reaches the forge.
- **`submit`** flushes the draft as a batched review, then clears it and
  re-fetches, so your just-posted comments come back as ordinary remote threads.

The cache is disposable (refetch any time); the draft is the precious part, and
it is the only thing "carry my review to another machine" needs to move.

## One-time setup

### A token for your forge

`fetch` and `submit` need a forge API token. This reuses `stack`'s configuration
exactly — put it in git config (per host is most precise; a blanket key works
too):

```sh
git config workflow.platform.github.com.token  ghp_xxx
git config workflow.platform.token             ghp_xxx     # less specific
```

or supply it through the environment:

```sh
export GITHUB_TOKEN=ghp_xxx     # or GITLAB_TOKEN
```

The forge (GitHub or GitLab in this version) is detected from the **upstream**
remote's URL, or `origin` when there is no upstream — the same role resolution
`stack` uses. A self-hosted instance behind a custom domain can be named:

```sh
git config workflow.platform.git.acme.com.service gitlab
git config workflow.platform.git.acme.com.api-url https://git.acme.com/api/v4
```

Purely local verbs (`show`, `comment`, `verdict`, `draft`, `diff`, `drop`,
`prune`) need only a remote to identify the repo — no token.

## The everyday loop

```sh
wits review fetch 123        # pull MR #123: metadata, objects, threads
wits review show 123         # read it (add --json for your editor)
wits review comment 123 --line src/x.c:42 <<'EOF'
This lock is taken twice on the error path.
EOF
wits review verdict 123 request-changes
wits review submit 123       # post it all as one review
```

Every authoring step only edits the local draft; `submit` is the one moment
anything reaches the forge. Preview exactly what it *would* post with `-n`:

```sh
wits review submit 123 -n
```

## Fetching

```sh
wits review fetch 123                       # one MR, by number
wits review fetch https://github.com/o/r/pull/123   # …or by URL
wits review fetch --feed mine               # a whole feed (see below)
wits review fetch --all                     # refresh every MR already fetched
```

Fetching one MR is a *full* pull (objects, threads, derived commit/file lists).
Fetching a feed is *light* — it refreshes only the inbox summaries, leaving the
expensive per-MR pull for an explicit `fetch <mr>`. That is what lets a feed
scale to a repo with thousands of open MRs.

### Feeds — an RSS-style subscription

A large project has more open MRs than you could ever pull, so batch acquisition
is a *subscription*. Feeds live in a single global TOML file
(`$XDG_CONFIG_HOME/wits/review.toml`, or `$WITS_REVIEW_CONFIG`), with a section
per repo keyed by its `host/owner/repo` identity:

```toml
[repo."github.com/mesa/mesa"]
feed.mine   = { reviewer = "@me", state = "open+draft" }
feed.vulkan = { labels = ["vulkan"], exclude-labels = ["wip"], limit = 30 }
```

- **Fields:** `state` (`open+draft` default, or `open` / `draft`; merged and
  closed are never fetched), `labels`, `exclude-labels`, `author`, `assignee`,
  `reviewer`, `search` (a raw platform search string, the full-text escape
  hatch), and `limit` (default 50).
- **`@me`** in `author`/`assignee`/`reviewer` resolves to you.
- **Filters are pushed down to the forge** and paginated server-side — never
  "fetch everything then filter locally".

A repo with no section simply has no feeds — but a token alone still lets you
`fetch <number>` any single MR, so feeds are an enrichment, not a requirement.

> Note: multiple `labels` are AND-ed (an MR must carry all of them) on both
> GitHub and GitLab — that is the platforms' own behaviour for a single list
> query. Use separate feeds when you want either-or.

## Reading

```sh
wits review show                 # the inbox: every fetched MR
wits review show 123             # one MR's full review state
wits review show 123 --json      # …as the stable editor payload
```

The detail view folds your pending draft into the remote threads, so you see one
merged picture: remote comments, your pending replies attached to their threads,
and your new comments as fresh local threads. For a large MR, filter instead of
paginate:

```sh
wits review show 123 --outdated   # threads whose anchored line moved
wits review show 123 --resolved   # resolved threads
wits review show 123 --unread     # threads whose last comment is someone else's
wits review show 123 --file src/x.c
```

Diff coordinates (the tool does not render diffs — your editor and `git` do):

```sh
wits review diff 123                 # commits + changed files of base..head
wits review diff 123 --range a1b2..c3d4
wits review diff 123 --patch         # the textual patch, via git (terminal/debug)
wits review diff 123 --json          # coordinates for an editor
```

`--range` accepts `all` (the whole `base..head`, the default), a git range
`A..B`, or a single revision.

## Authoring a review

All of these only edit the local draft. The comment body comes from a file
positional, or from stdin when omitted or given as `-`:

```sh
# a line comment (body on stdin)
wits review comment 123 --line src/x.c:42 <<'EOF'
Off-by-one: this should be `<=`.
EOF

# a multi-line anchor (lines 40–42), body from a file
wits review comment 123 --line src/x.c:42 --start-line 40 note.md

# a comment on the "old" (deleted) side
wits review comment 123 --line src/x.c:10:old  -

# a file-level comment (no line)
wits review comment 123 --file src/x.c  -

# an MR-level conversation comment (no code anchor)
wits review comment 123 --mr-level  -

# a reply to an existing thread (its id is shown by `show`)
wits review comment 123 --reply remote:9987  -

# edit the body of a pending draft comment/reply (its local id)
wits review comment 123 --edit local:2  -

# drop a pending draft action
wits review drop 123 local:2
```

Set the verdict (one per MR); an optional summary reads from a body source, so a
bare verdict never waits on stdin:

```sh
wits review verdict 123 approve
wits review verdict 123 request-changes summary.md
wits review verdict 123 comment  -
```

Resolve or unresolve a thread (recorded into the draft, applied at submit):

```sh
wits review resolve   123 remote:9987
wits review unresolve 123 remote:9987
```

Inspect the pending draft at any time:

```sh
wits review draft 123          # human
wits review draft 123 --json   # machine
```

## Submitting

```sh
wits review submit 123          # one MR
wits review submit 123 --stack  # every drafted MR in 123's stack
wits review submit --all        # every MR that has a pending draft
```

A single MR's review — verdict, summary, and all line/file comments — is posted
as **one** call where the platform allows it (GitHub's review API; GitLab's draft
notes bulk-published, plus a separate `approve` for an approving verdict), so it
triggers a single notification. MR-level conversation comments, replies, and
resolves are separate calls.

Reconciliation is **per action**: whatever lands is cleared from the draft,
whatever fails stays for a retry. Only when a draft empties completely does the
tool re-fetch, so a partial failure never loses your unposted comments.

## Reviewing the code itself: `checkout`

To actually build, run, or fuzz an MR, materialize its code:

```sh
wits review checkout 123               # into a worktree (leaves your tree alone)
wits review checkout 123 --worktree /tmp/mr123
wits review checkout 123 --in-place    # in the current tree (moves HEAD)
```

The default is a **worktree** at a sibling path (`../<repo>.review/mr-<id>`),
which lets you review several MRs at once. `--in-place` checks the snapshot out
in your current working tree; because that moves `HEAD`, it **refuses a dirty
tree** — reviewing someone else's work must never bury yours.

Walk a stack without naming each MR (relative to the last checkout):

```sh
wits review checkout --next     # the MR one step up the stack
wits review checkout --prev     # one step down
```

The stack shape is reconstructed from the fetched MRs' base/source branches, so
this works for anyone's stack — no `.git/machete` needed.

## Housekeeping: `prune`

An MR's local footprint is small, but the git objects its snapshots pin
(`refs/wits/review/*`) add up. `prune` drops the cache, draft, and pins of
terminal MRs and lets git collect the objects:

```sh
wits review prune                 # merged/closed MRs
wits review prune --older-than 30 # …and any not refreshed in 30 days
```

Like `stack tree prune`, it is idempotent and a no-op when nothing is stale, so
running it on a timer is harmless.

## Outdating

A review is always pinned to the snapshot you fetched. Comments anchor to that
snapshot's commit, and `submit` posts them against it — so when the branch has
moved on, the forge shows them as *outdated* rather than silently re-basing them
onto code they were never about. `show --outdated` surfaces threads whose
anchored line has left the current diff. The objects you reviewed are held alive
by `refs/wits/review/*` even after the author force-pushes, so an outdated
comment can still be submitted.

## Configuration reference

| What | Where | Notes |
|---|---|---|
| Forge token | git config `workflow.platform.*` / `GITHUB_TOKEN` / `GITLAB_TOKEN` | Shared with `stack`; see [stack.md](stack.md). |
| Service / API override | git config `workflow.platform.<host>.service` / `.api-url` | Self-hosted instances. |
| Feeds | `$XDG_CONFIG_HOME/wits/review.toml` (or `$WITS_REVIEW_CONFIG`) | Per-repo `[repo."host/owner/repo"]` sections. |
| Store location | `$WITS_REVIEW_DIR` > `$XDG_STATE_HOME/wits/review` > `$GIT_DIR/wits/review` | State/cache, distinct from config. See [store.md](review/store.md). |

Per-run choices (`--range`, `--stack`, `--all`, `-n`) are flags, not config,
because they describe one invocation.

## Version scope and limitations

This first version is deliberately bounded, and honest about it:

- **Forges:** GitHub and GitLab. Gitea/Forgejo/Codeberg have the trait seam but
  no backend yet.
- **Thread resolution** is supported on **GitLab only**; GitHub's is GraphQL-only
  and deferred (the tool speaks REST). `resolve`/`unresolve` still record into a
  draft, but submitting them to a GitHub MR reports the gap.
- **`request-changes` on GitLab** has no native equivalent; it maps to "post the
  review and do not approve".
- **Editing or deleting an already-published comment** isn't supported yet;
  `--edit`/`drop` act on *pending* draft actions only.
- **Anchoring to an older snapshot:** if you re-`fetch` an MR after writing
  comments and its head moved, `submit` warns that anchors may be off — it posts
  against the currently-held snapshot. Reviewing and submitting before a refresh
  is the smooth path.
- **Feeds** pull the most recently-updated MRs up to `limit`; an incremental
  "only since last sync" cursor is future work.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `no 'origin' or 'upstream' remote…` | `review` keys everything off the target remote; add one. |
| `no API token for …` | Set `workflow.platform.<host>.token` or the `*_TOKEN` env var (fetch/submit only). |
| `MR N isn't in the store yet` | Run `wits review fetch N` first — local verbs read the cache. |
| `no feed 'x'` | The repo has no `[repo."host/owner/repo"]` / `feed.x` in `review.toml`. |
| `working tree has uncommitted changes` (checkout) | In-place checkout moves HEAD; commit/stash first, or use a worktree. |
| A GitHub thread won't resolve | Deferred in v1 (GraphQL-only); resolve it in the web UI for now. |
| `some actions did not submit` | A per-action failure; the failed ones stayed in the draft — fix and re-`submit`. |

## Invocation forms

Like every `wits` tool, `review` has a direct form via symlink — `wits-review` —
created by `meson install` (see the top-level [README](../README.md)).
