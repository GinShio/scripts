# `wits review` — Design

> Status: **implemented.** This records the agreed shape and the *why* behind it;
> the code lives in `crates/wits/src/cmd/review/` and the review half of
> `crates/wits-util/src/forge/`. Where a detail evolved during implementation the
> code is authoritative and this file has been kept in step (see §17 for what v1
> deliberately scoped out).
>
> This file explains *why the tool is shaped the way it is*. The companion usage
> document (`docs/review.md`) explains *how to drive it* and carries the full,
> reader-facing reference; the editor contract is in `docs/review/json.md` and the
> on-disk store in `docs/review/store.md`. Neither restates the other;
> behaviour-for-users goes there, rationale goes here.

---

## 1. What the tool is, and what it deliberately is not

`review` is the mirror image of `stack`. `stack`'s creed is *local topology is
given to us; we own the remote* — it manages the **existence and structure** of a
set of MRs. `review` manages the **review content** of an MR: the diff a reviewer
reads, the threads they leave, the verdict they render.

> One tool's job stated in one sentence:
>
> **`review` owns a local, plain-text model of review state (a pinned snapshot,
> its threads, and a pending verdict), and reconciles it against the forge. It
> never owns the code, never rebases, never pushes branches — `git` and `stack`
> do that.**

Two consequences fall straight out of that framing, and they are the spine of
everything below:

- **The target is any MR in the repo, not just my own stack.** I review other
  people's work far more than I re-read mine, so the tool cannot assume a local
  branch, a `.git/machete` entry, or authorship. Acquisition is therefore
  **forge-first** (§4): address an MR by number, ask the forge what it is, fetch
  its objects. Machete/`stack` integration (§13) is a convenience for reviewing
  *my own* stacks, not a prerequisite.
- **A review is pinned to a snapshot, and we never assume HEAD is latest.** The
  natural sequence is `commit A → I review → author pushes B, C → I sync → I
  submit`. My comments were about the code at `A`; they must be submittable
  against `A` even though the branch has moved. Snapshots and outdating are
  therefore first-class (§5, §6), not an afterthought.

Non-goals, stated once: no diff *rendering* (the editor and `git` do that; we own
diff *coordinates*, §5.2), no rebase/restack engine, no conflict resolution, and
no multi-user collaboration layer (the forge is that layer — our local store is
for carrying *my own* in-progress review across *my own* machines, §7).

(On naming: GitHub says PR, GitLab says MR. Internally and throughout this
document we say **MR**; the user-facing noun is a per-forge presentation detail,
already supplied by `Forge::noun`.)

## 2. CLI surface

The verbs collapse to a small set once one principle is applied: **only two verbs
touch the network.** Everything a reviewer does — comment, reply, edit, drop,
set a verdict, resolve a thread — is recorded as a *pending action* in a local
draft, and nothing reaches the forge until `submit` flushes the draft in one
batch. This is what keeps a review from spraying a notification per keystroke,
and it is the same "reconcile, don't stream" discipline `stack` uses.

```
# Network — the only two verbs that talk to the forge.
wits review fetch  [mr | feed | --all]     # idempotent: first pull or refresh; pins objects
wits review submit [mr | --stack]          # flush the recorded draft actions, batched

# Authoring — record actions into the local draft; no network.
wits review comment <mr> (--line P:L[:side] | --file P | --mr-level | --reply ID | --edit ID) [FILE]
wits review verdict <mr> {approve|request-changes|comment} [FILE]
wits review drop    <id>
wits review resolve   <thread>             # GitLab only in v1 (§10)
wits review unresolve <thread>
wits review draft   [mr]                   # inspect what the draft will submit

# Reading — from local cache, no network; everything supports --json.
wits review show [mr] [--outdated] [--resolved] [--unread] [--file P]
wits review diff <mr> [--range S]          # coordinates/anchors; --patch shells to git

# Materialize / navigate / housekeep.
wits review checkout <mr|stack> [--next|--prev]   # into a worktree or in-place, via project
wits review prune [--older-than …]
```

The split earns its keep the same way `stack`'s does: when `submit` fails on the
third of five MRs you want to know exactly where you were and re-run only that,
and `fetch`/`show`/`diff` are pure reads you can lean on freely.

Notes on shape, each with its reason:

- **`fetch` subsumes "pull" and "sync".** They are the same operation — bring the
  local view in line with the forge — differing only in whether it is the first
  time. Like `git fetch`, it is idempotent; there is no reason for two verbs.
- **`show` with no MR is the inbox; with an MR it is the detail view.** There is
  no separate `list`: the inbox is just "show me everything I've fetched", and
  the human print is secondary to `--json` (§12), which the editor consumes.
- **Bodies come from a positional `FILE`, defaulting to stdin (`-`).** This is
  more Unix-idiomatic than a `--body-file` flag and sidesteps the multi-line
  markdown quoting hell of `-m`. `comment`, `verdict`, and `comment --edit` all
  read their body this way.
- **Navigation is a flag on `checkout`, not a verb.** `--next`/`--prev` move
  along the stack relative to the currently checked-out MR (§13); `show`/`diff`
  take an explicit `<mr>`, since the editor computes "next" from the `neighbors`
  block `show` already returns.
- **`resolve`/`unresolve` and `verdict` are draft actions, not network calls.**
  They are recorded and applied at `submit`, so the whole review lands as one
  intent.

Global `-v/--verbose` and `-n/--dry-run` come from the `wits` process layer for
free; every network call respects dry-run, every read still runs (`submit -n`
prints "would post these threads with this verdict to this MR").

## 3. Code organization

`review` follows `stack`'s `core`/`util`/`cmd` line and reuses that layer whole.
The command's own logic lives under `cmd/review/`; the git-hosting concerns it
leans on stay in `util/` (`forge`, `remote`) and grow there (§10). Concretely:

- **Reuse unchanged:** `wits_util::remote` (URL parsing, origin/upstream roles),
  `forge::detect` (which platform, which token), the `Repository` git floor, the
  `log`/`process` dry-run machinery, and `stack::resolution` for the "what is a
  stack" question when reviewing my own (§13).
- **Extend:** the `Forge` trait gains a review-facing half (§10) — added to the
  same trait, not a parallel one, so "add a forge" stays one self-contained
  mapping.
- **New, under `cmd/review/`:** the review model (§5), the local store (§7), the
  feed engine (§9), the anchor/outdate logic (§6), and the JSON contract (§12).

## 4. Acquisition — forge-first, and how objects are kept alive

Because the target is any MR, acquisition inverts `stack`'s branch-first model.

**Addressing.** The primary handle is the **MR number** (a URL is also accepted
and parsed to the same thing). Within one checkout we talk to exactly one target
repo — resolved as `stack` does, `Remotes::resolve` then `forge::detect` — and an
MR number is unique within that target, so the number alone is a complete
address. Batch acquisition names a **feed** instead (§9).

**Fetching objects, including across forks.** The forge exposes an MR's head
under a special ref on the *target* repo — `refs/pull/<n>/head` (GitHub),
`refs/merge-requests/<iid>/head` (GitLab) — and this works even when the source
branch lives in a fork we have no remote for. So `fetch` issues an explicit
refspec against the target remote (these refs are not in the default fetch set)
and pulls the snapshot's objects down. The fork's `owner/repo` never has to be a
local remote.

**Keeping snapshots alive without depending on another tool.** Once the author
force-pushes and we `fetch` the new head, the old snapshot's commit is no longer
reachable from any branch and becomes a GC candidate. We keep it alive by holding
our own ref:

```
refs/wits/review/<mr>/<snapshot-sha>        # → the snapshot's head commit
refs/wits/review/<mr>/<snapshot-sha>-base   # → its base, when not an ancestor of head
```

- The ref name carries only the **MR number** and the **snapshot SHA** — the two
  things that are actually required for uniqueness within a clone. Host and
  `owner/repo` are contextual to the target remote and live in the cache
  metadata, not the ref path.
- These refs *are* the record of "which snapshots we have pinned locally"
  (enumerable with `git for-each-ref`), so nothing duplicates that list into the
  MR's cached metadata (§7). Three distinct concerns, three distinct sources:
  **refs** = what we pinned; the **remote cache** = the forge's version history;
  the **MR info** = a pointer to the snapshot currently under review.
- This is unconditional. It does **not** care whether `git-branchless` is
  installed — a tool's behaviour must not depend on whether another tool happens
  to exist. When branchless is present we simply coexist with its refs; when it
  isn't, ours are the whole mechanism.

`prune` (§15) is the other end of this: it deletes these refs when a snapshot is
no longer needed, letting git GC the objects.

## 5. The review model

Four types, no more, and each maps onto something a forge can actually represent.

### 5.1 `Snapshot` — outdating made structural

A review is always pinned to `Snapshot { base_sha, head_sha }` — the pair we
diff and the SHAs comments anchor against. **Branch outdate** is then just
`reviewed.head_sha != current head`; **comment outdate** is just "the anchored
line does not exist at the current head." Neither needs a state machine; both are
inferences from "the pinned snapshot is no longer the tip." We never assume the
tip is latest, so submitting against an old snapshot is the normal path, not an
error path (§6).

For GitLab, a snapshot also carries the forge's version SHAs
(`base_sha / start_sha / head_sha`) captured at `fetch`, because GitLab's comment
`position` requires them and they are not derivable from local git alone (§10,
capability A1).

### 5.2 `Anchor` — file coordinates, computed locally, translated at submit

```
Anchor {
    commit_sha,          // the commit the comment is about (an old one → outdated on submit)
    path,                // new_path; old_path also kept for renames/deletes
    side: Old | New,     // which side of the diff; default New, Old only for deleted lines
    line,                // the file line number on that side
    range: Option<(u32, u32)>,  // multi-line selection
}
```

The deliberate choice is **file line numbers, not diff-hunk positions.** Modern
forges anchor by file line (GitHub now; GitLab's `old_line/new_line`), so an
anchor expressed in file coordinates lines up with whatever diff the editor
renders — the tool and the editor never have to agree on one canonical diff text.
The tool computes diffs internally only to *judge* things (is this line changed,
which commit last touched it, is this anchor now outdated), never to *render* for
the user. `review diff` therefore emits coordinates and anchors; only
`diff --patch` shells to `git` for a terminal convenience.

### 5.3 `Thread` + `Comment`, and the three placements

```
Thread  { id, placement, resolved: bool, outdated: bool, comments: Vec<Comment> }
Comment { id, author, body, origin: Local | Remote, created_at, state: Pending | Published }
```

A thread's **placement** is one of three, and the boundary between them is a
forge reality, not a preference:

- **`line`** — anchored to a code line (a review comment). Allowed on any line of
  a file the MR *touches*, including unchanged/context lines — this is the
  "comment outside the diff hunk" requirement, and both target forges now support
  it (GitHub dropped the in-hunk restriction; GitLab shows the full changed
  file).
- **`file`** — anchored to a changed file but not a line (`subject_type: file`).
- **`mr`** — the MR-level conversation comment (GitHub's issue comment; a GitLab
  note with no position). This is "just leave a remark on the MR."

A line/file comment can only land on a file the MR changed; anything else is an
`mr` comment. IDs are origin-prefixed (`local:3`, `remote:9987`) so the editor
can address both; local IDs are a per-draft counter (§7).

### 5.4 `Draft` — a set of pending actions, not just comments

```
Draft {
    verdict: Option<Approve | RequestChanges | Comment>,
    summary: Option<Body>,     // the review body; rides with the verdict, no extra notification
    actions: Vec<Action>,      // AddComment | Reply | Edit | Drop | Resolve | Unresolve
}
```

The draft is the **only** mutable local state, and it holds *intent*, not just
new comments: editing or dropping an already-published comment, replying to a
remote thread, resolving a discussion — all are actions recorded here and applied
at `submit`. One draft per MR (so reviewing a stack means several drafts); the
verdict is one per MR.

## 6. Outdating — anchor to what you reviewed, let the forge mark it

The governing rule: **submit each comment against the SHA it was written on, and
let the forge display it as outdated.** We do not auto-re-anchor a comment onto
the new line — that risks pinning it to the wrong code. GitHub accepts a comment
with `commit_id` set to the reviewed commit (marked outdated when HEAD has moved);
GitLab accepts a `position` built from the reviewed version's SHAs. This is
exactly the "outdated comments can still be pushed" requirement, and it is why §4
pins the snapshot's objects and §5.1 keeps the version SHAs.

The GC edge — reviewing `A`, sitting on the draft for a very long time, the author
force-pushing `A` away, and the forge eventually GC-ing it — is real but bounded:
we hold `A` locally regardless, forges retain pushed commits for a long time, and
the moment we first submit, our own comment references `A` and pins it on the
forge forever. If a submit against a vanished commit ever does fail, it is handled
per-action (§11): that comment stays in the draft with a warning, the rest go.

Moving a stale local draft onto the new state is offered only as an explicit,
opt-in convenience — never the default, never a correctness dependency. The
default is honesty: the comment stays anchored to the code it was written about.

## 7. Local store — a disposable cache and a precious draft

Two kinds of data with opposite lifetimes must not share a representation, or the
store rots:

- **`remote/mr-<id>.json`** — the forge's state as we last observed it: threads,
  their authors and resolved/outdated flags, the diff version list, the MR's
  metadata. This is a **cache**: refetchable at will, safe to overwrite whole,
  never the source of truth. Everyone else's comments live here.
- **`draft/mr-<id>.json`** — the pending actions and verdict for one MR (§5.4).
  This is the **precious** part: it is what would be lost, and it is what "carry
  my review to another machine" (portability, not collaboration) actually moves.

**Submit clears the draft.** Once a draft is flushed, everything in it is public
on the forge, so the local draft has nothing left to hold — we clear the
succeeded actions and re-`fetch` the remote cache, and the just-submitted
comments come back as ordinary remote threads. This is the `prr` model —
author a batch, submit, done — and it is why we need **no identity-stitching**
between a local pending comment and the remote thread it became: after submit,
there is no local pending comment to stitch. Local IDs are a per-draft counter,
never reused after a drop (tombstone), reset when the draft clears.

The store root follows the env → XDG → GIT_DIR ladder the `config` floor already
provides, with **state** kept distinct from **config** (§8):

```
WITS_REVIEW_DIR  >  $XDG_STATE_HOME/wits/review  >  $GIT_DIR/wits/review
```

The default is `$GIT_DIR/wits/review`, per-clone like `.git/machete`; env/XDG lift
it out when you want it centralized or shared across clones.

## 8. Configuration — git config for secrets, TOML for feeds

Two axes, split by what each is good at:

- **Identity and secrets** — token, service override, api-url — stay in **git
  config** under `workflow.platform.*`, reused verbatim from `stack`. Per-host,
  per-repo, and appropriate for a secret. `review` needs nothing new here.
- **Review behaviour** — feeds and their filters (§9), cache policy — goes in
  **TOML**, because it is structured and will grow, and git config is a poor home
  for nested lists.

The TOML is a **single global file** with per-repo sections, borrowing `prr`'s
"one global config, target on the command line" shape but keyed by the parsed
remote identity so it can hold many repos:

```toml
# $XDG_CONFIG_HOME/wits/review.toml   (overridable via WITS_REVIEW_CONFIG)

[repo."github.com/mesa/mesa"]
feed.mine   = { reviewer = "@me", state = "open+draft" }
feed.vulkan = { labels = ["vulkan"], state = "open+draft" }
```

Two deliberate points:

- **Config (`XDG_CONFIG`) and state (`XDG_STATE`, §7) live in different trees** —
  the correct XDG split, and it keeps a backup of one from dragging in the other.
- **Graceful degradation, as in `stack`.** A token alone is enough to
  `fetch <number>` and review any single MR anywhere; the TOML only adds the
  *batch/subscription* layer. "A repo without config isn't supported" means "has
  no feeds", not "can't be reviewed". Personal review preferences are therefore
  never committed into someone else's repo.

## 9. Feeds — an RSS-shaped subscription, filtered server-side

A repo like mesa or llvm has more open MRs than we could ever fetch, so batch
acquisition must be a *subscription*, not "fetch everything then filter". A feed
is a named set of faceted filters:

- **Fields:** `state` (defaults to `open+draft`; `merged`/`closed` are not
  fetched), `label`, `author`, `assignee`, `reviewer`.
- **Semantics:** different fields are **AND** (`reviewer=@me` *and*
  `label∈{…}` *and* `state∈{open,draft}`). This is the faceted model
  `gh pr list` / `glab mr list` and the forges' own search use; a full expression
  language would be over-built for the need. (In v1 multiple *labels* are AND-ed
  on both GitHub and GitLab — that is the platforms' behaviour for one list/search
  query; the earlier hope of within-field OR for labels isn't natively available
  and client-side union was rejected for scale.)
- **Negation:** per-field `!=` is supported (`label != wip`), which is the one
  extension that pays for itself (dropping bot/WIP noise).
- **Escape hatch:** a `search = "..."` string is passed straight to the
  platform's search endpoint for the rare full-text case, so we never invent a
  query syntax.

The load-bearing decision: **filters are pushed down to the forge's list/search
query and paginated server-side** — never applied client-side after a full fetch.
And feed refresh is **incremental** (an `updated_at` cursor, only MRs touched
since the last sync) with a hard cap, or the inbox still explodes on a large repo.

`prr` has no filtering (it names specific PRs), so it is no guide here; the real
analogues are the forge `list` CLIs and an RSS reader's filter rules.

## 10. Forge extension and the honest capability matrix

The review half is **added to the existing `Forge` trait**, not split into a
parallel one, so adding a platform stays a single self-contained mapping. The new
primitives, sketched:

```rust
// Added to Forge, alongside the existing find/create/set_base/set_body/apply_attributes:
fn list_threads(&self, mr: &str) -> Result<Vec<RemoteThread>>;   // + resolved/outdated flags
fn submit_review(&self, mr: &str, draft: &ReviewDraft) -> Result<SubmitOutcome>;
fn reply(&self, thread: &RemoteThreadId, body: &str) -> Result<()>;
fn resolve(&self, thread: &RemoteThreadId, resolved: bool) -> Result<()>;   // GitLab only in v1
```

As with the MR half, no platform JSON shape escapes a host module. But review
touches corners of the platforms that do **not** normalize cleanly, and pretending
otherwise would be the mistake. These are documented as a matrix rather than
hidden, because a reviewer needs to know them:

| Concern | GitHub | GitLab |
|---|---|---|
| Batched review (comments + verdict) | one review call → one notification | draft notes → one bulk publish |
| `approve` verdict | part of the review call | a **separate** approve endpoint (so review submit is 2 calls) |
| `request-changes` verdict | native | **no native equivalent** — mapped to "post review, leave unapproved" (§ below) |
| Diff-line comment anchor | `commit_id` + file line; commit must be in the PR | `position{base/start/head_sha, old/new_line}`; commit must be a known MR *version* (A1) |
| Comment on unchanged line of a changed file | supported | supported |
| File-level comment | `subject_type: file` | note without line |
| MR-level (conversation) comment | issue-comment API (separate object/notification) | position-less note |
| Reply to an existing thread | often a separate call, not part of the batch | note added to the discussion |
| Resolve / unresolve a thread | **GraphQL-only** → deferred to future in v1 | REST, supported in v1 |

The named caveats behind the matrix:

- **A1 — GitLab diff anchors need forge SHAs.** A line/file comment on GitLab can
  only target a commit the forge knows as an MR *version*. Normal review (of
  pushed MR commits) always satisfies this; commenting on an un-pushed local
  intermediate commit does not, and degrades to an `mr` comment. GitHub is more
  lenient. Captured by keeping the version SHAs on the snapshot (§5.1).
- **A3 — `request-changes` on GitLab** is not a first-class action. It maps to
  "submit the review as a comment and do not approve" (optionally unapprove). The
  verdict is fundamentally a GitHub/Gitea concept; the doc says so plainly rather
  than faking parity.
- **A5 — batching is best-effort per platform.** New inline comments batch into
  one review; replies to existing threads and (GitLab) resolves may be separate
  calls with their own notifications. The tool minimizes notifications where the
  platform allows and does not promise more than it can deliver.

Transport stays **REST over `ureq`** — the v1 choice to defer GitHub thread
resolution (which would need GraphQL) exists precisely to keep it that way.

## 11. Submit — batched, concurrent, reconciled per action

`submit` flushes drafts, and its correctness rests on getting the granularity
right:

- **Concurrency is per-MR and unconstrained.** Unlike `stack submit` (which
  serializes sibling MR *creation* to dodge duplicate-detection races), review
  submissions to different MRs are wholly independent, so they fan out over the
  same bounded thread pool with no ordering constraint.
- **Atomicity is per-action, not per-MR.** A single MR's draft can expand to
  several forge calls — the batched review, a separate `mr` conversation comment,
  a GitLab approve, a resolve — so a partial failure within one MR is possible.
  `submit` therefore tracks each action's outcome, clears the ones that landed,
  and leaves the failed ones in the draft to retry. (This corrects an earlier,
  looser "per-MR atomic" framing.)
- **The local write happens once, after the fan-out joins.** All network work
  runs first; only when every task has returned does `submit` reconcile the local
  drafts in a single pass. There is never a half-cleared draft and never two
  writers racing on the store.

## 12. Editor interface — one contract, and it is `--json`

The editor is a client of the tool, and the boundary is drawn to keep the store
free to evolve:

- **The editor reads only through `--json`, never the store files.** The on-disk
  layout (§7) is a private implementation detail; the stable contract is the JSON
  emitted by `show`/`diff`/`draft`. Coupling an editor to the disk schema would
  freeze the schema forever; a `--json` API is cheap and versioned (every payload
  carries a `schema` integer).
- **The editor writes only through subcommands.** A comment, an edit, a drop, a
  verdict — each is an ordinary CLI invocation with the body on stdin. The editor
  never POSTs a JSON blob in; the write surface stays small, argument-shaped, and
  trivially fuzzable.
- **`show` returns the whole MR in one payload, and filtering is the knob.**
  Even a large MR is a small JSON document and cheap I/O; pagination is not worth
  the complexity. What is worth it is *filtering* — `--outdated`, `--resolved`,
  `--unread`, `--file` — so the editor (or a terminal user) can pull just the
  threads that matter.

Sketch of the two payloads (fields illustrative, `schema`-versioned):

```json
// show <mr> --json
{ "schema": 1,
  "mr":       { "mr": "123", "display": "#123", "title": "…", "state": "open", "draft": false },
  "snapshot": { "base_sha": "…", "head_sha": "…" },
  "neighbors":{ "position": 2, "prev_mr": "122", "next_mr": "124", "nodes": ["121","122","123","124"] },
  "commits":  [ { "sha": "…", "parent": "…", "subject": "…" } ],
  "files":    [ { "path": "src/x.c", "old_path": null, "status": "modified" } ],
  "threads":  [ { "id": "remote:9987", "origin": "remote", "resolved": false, "outdated": true,
                  "placement": { "kind": "line", "path": "src/x.c", "side": "new",
                                 "line": 42, "range": [40,42], "commit": "A_sha" },
                  "comments": [ { "id": "remote:5", "author": "alice", "origin": "remote",
                                  "body": "…", "state": "published", "created_at": "…" } ] } ],
  "draft":    { "verdict": "comment", "summary": "…" } }
```

A long-running `serve` daemon speaking JSON-RPC is a **possible future**
optimization — it would keep parsed diffs, computed anchors, and a warm forge
session in memory, push outdate/CI changes to the editor, and serialize writes
natively. It is explicitly *not* v1: the whole codebase is currently zero-IPC, and
a daemon is a cache layer over the *same* `--json` contract, not a redesign, so it
can be added later without breaking anything.

## 13. Stack integration — navigate freely, but a comment belongs to one MR

Reviewing a stack is jumping between its MRs, and the tool should make that fluid
without inventing anything the forge can't store.

- **Stack shape is reconstructed from the MR list, not required from machete.** A
  chain of MRs whose base branches link head-to-tail *is* a stack; we read it off
  the feed/`fetch` results, so `review` is stack-aware for *anyone's* stack, not
  just mine. My own `.git/machete` (via `stack::resolution`) is one possible
  source, not a prerequisite. Re-deriving the shape happens when the MR list is
  refreshed (`fetch`), so a rebase that reshapes the stack is picked up
  explicitly rather than guessed at.
- **Navigation, not cross-MR comments.** `checkout --next/--prev` walks the chain
  (relative to a small per-worktree pointer recording the current review, §14),
  and `show` hands the editor a `neighbors` block so it can do the same. But a
  **comment always belongs to exactly one MR** — the forge has no object for a
  comment spanning two MRs, and faking one would be an abstraction with nowhere to
  push. A "stack review" is therefore a connected *session* — per-MR drafts you
  build while hopping nodes and then `submit --stack` in one go — not a shared
  comment surface. Each MR keeps its own verdict, which is usually what you want
  (approve the base, request changes up top).

## 14. Materialization — worktree or in-place, via `project`

To debug/build/fuzz/test an MR's code, the tool must put it somewhere runnable
without clobbering your current work. `checkout` delegates to `project`'s existing
policy rather than inventing a third:

- **Worktree mode:** spin a worktree for the MR (or the stack — one worktree holds
  a whole chain, the natural granularity), leaving your main tree untouched. This
  is what lets you review several MRs at once.
- **In-place mode:** check the snapshot out in the single working tree. This is
  *supported on purpose* (not everyone uses worktrees), but it moves HEAD and can
  only host **one** active review at a time, so it is explicit and **hard-guards a
  dirty tree** — reviewing someone else's MR must never silently bury your work.

Naming and reclamation of the worktree/context are `project`'s to own; `review`
only borrows the context and records, per worktree, which MR/stack is currently
checked out (so `--next/--prev` has an origin, §13).

## 15. Lifecycle and `prune`

An MR ends its life two ways, and only one is unambiguous:

- **Explicit (merged/closed).** `fetch` observes the terminal state and marks it;
  such MRs are the clear targets of `prune`.
- **Implicit (long dormant).** Never auto-deleted — we can't be sure it's dead —
  but reachable by an optional `--older-than`.

The cost of *not* pruning is deliberately bounded so that doing nothing is fine:
the remote cache is kilobytes, and the only real weight is git objects held alive
by the `refs/wits/review/*` pins (§4). So we pin objects **only for MRs with an
active local draft or an explicit checkout**; a merely-listed MR pins nothing, and
a submitted-and-cleared draft releases its ref. `prune` then mirrors
`stack tree prune`: idempotent, automatable, a no-op when nothing dangles —
dropping the refs of terminal MRs and letting git GC the objects.

## 16. Rejected alternatives

- **Depend on the forge's rendered diff.** Rejected: computing diffs from fetched
  objects locally keeps us offline-capable, consistent with local content, and
  consistent with `stack`'s fidelity argument for the `git` CLI. We own
  coordinates; the editor renders.
- **A parallel `ReviewForge` trait.** Rejected: it would make "add a forge" two
  mappings in two places. Extending the one `Forge` trait keeps it cohesive.
- **A daemon/RPC protocol in v1.** Rejected as premature (§12); it is a future
  cache layer over the same JSON contract, not a foundation.
- **A mutable, in-place JSON blob per MR.** Rejected in favour of the
  cache/draft split (§7): conflating the disposable remote view with the precious
  local intent is how the store rots.
- **An append-only event log for the local draft.** Considered and dropped once
  "submit clears the draft" (§7) removed the need for durable history or
  identity-stitching. The draft is small, short-lived, and single-writer; a plain
  document is simpler and loses nothing.
- **Auto-re-anchoring outdated comments onto the new line.** Rejected as a default
  (§6): it can pin a comment to the wrong code. Honesty (anchor to the reviewed
  SHA) is the default; re-anchoring is an explicit opt-in.
- **Cross-MR comments on a stack.** Rejected (§13): no forge object backs it.
- **Folding `review` into `wits stack`.** Rejected: it is its own subcommand with
  its own verbs; it only *reuses* `stack`'s resolution.

## 17. What v1 scoped out, and future work

Delivered in v1: forge-first acquisition with object pinning, the
snapshot/anchor/thread/draft model, the cache+draft store on the
env→XDG_STATE→GIT_DIR ladder, config-driven feeds, the `--json` contract
(`schema` 1), worktree/in-place materialization with stack navigation, `prune`,
and the GitHub + GitLab review backends.

Deliberately deferred, and honest about it:

- **future** GitHub thread resolve/unresolve, once a minimal GraphQL path is
  worth adding (v1 is REST-only, so resolve works on GitLab only — §10).
- **future** editing or deleting an *already-published* comment; v1's `--edit`
  and `drop` act on pending draft actions only.
- **future** per-comment historical version tracking so an outdated draft submits
  against the exact snapshot each comment was written on; v1 submits against the
  currently-held snapshot and warns when a comment's stamped SHA differs (§6).
- **future** the incremental-sync cursor for feeds (v1 pulls the most-recently
  updated MRs up to `limit`; the `updated_after` plumbing exists but is unused —
  §9), and a feed cache-expiry policy.
- **future** Gitea/Forgejo/Codeberg review backends (the trait leaves the seam,
  §10).
- **future** a `serve` daemon over the `--json` contract for large-MR latency and
  live outdate/CI push (§12).
- **future** CI status surfaced into `show` (shared with `stack`'s own deferred CI
  read-back).
