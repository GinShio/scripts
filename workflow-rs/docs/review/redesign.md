# `wits review` — API-native revision

> Status: **implemented.** This document is the rationale for the revision that
> reshaped the *forge boundary*: how a review is submitted and read back, grounded
> in the **current** GitHub and GitLab APIs rather than a lowest-common-denominator
> shape smeared over both. The spine of [`design.md`](design.md) (forge-first
> acquisition, snapshot pinning, the three-file store, the `--json`/`local.json`
> contracts, the local/network split) is unchanged; `design.md`'s §6/§10/§11/§17
> have been brought in step with what this revision delivered.
>
> Every API claim below was verified against the vendor documentation in 2026-07;
> the exact fields are quoted in §2. What could **not** be exercised in-tree is a
> *live* call to either forge (the test suite is offline by design): the mapping
> code is pinned by fixture unit tests, but the end-to-end network paths — GitHub
> GraphQL mutations, GitLab `bulk_publish` — still want a one-time live smoke test,
> as do three specific assumptions flagged in §9.

---

## 0. Why revise

The v1 forge layer picked one intermediate shape — *"a batch is a verdict +
summary + line/file comments; everything else is a separate call"* — and bent
each backend to fit. Confronting it with the real APIs shows the shape matches
**neither** platform:

- **GitLab can batch far more than that.** Its draft-notes + `bulk_publish`
  primitive natively carries line comments, file comments, MR-level notes,
  replies, thread resolutions, the summary body, **and** the verdict — the whole
  review, one atomic publish, one notification. v1 instead posts the summary as a
  lone draft, publishes with an empty body, then fires *separate* calls for
  replies, resolves, and approve/unapprove. It also documents
  `request-changes → unapprove`, which is simply **wrong now**: GitLab has a
  native `requested_changes` reviewer state.

- **GitHub REST can batch less than that.** The REST review endpoint
  (`POST …/pulls/{n}/reviews`) accepts only line comments in its `comments[]`
  (no `subject_type`), so v1's file-level comments — sent with
  `subject_type:"file"` inside that batch — are silently dropped or rejected.
  Worse, REST cannot read a thread's resolved state, cannot resolve, groups
  threads by a fragile `in_reply_to_id` walk, and reads *outdated* off a
  `line == null` heuristic. And the feed uses `search/issues`, which returns
  **issue** shells with no `base`/`head`/`head_sha` — losing stack links and the
  head SHA, and conceptually querying the wrong object.

The fix is to invert the dependency: **let each `Forge` own the mapping of a
single, rich review batch onto its native primitive, and report honestly what it
could and couldn't do.** The orchestration layer stops assuming a shape and just
hands over intent.

Two platform decisions (agreed with the owner) anchor the revision:

1. **GitHub review backend goes GraphQL.** GraphQL is where GitHub actually
   models reviews: threads (grouping, `isResolved`, `isOutdated`), resolution
   mutations, and a `search` that returns real `PullRequest` objects. REST stays
   only for the object-fetch refspec and (unchanged) the `stack` half.
2. **GitLab submit becomes one `bulk_publish`**, with a graceful fallback to the
   v1 multi-call path for instances too old to have `reviewer_state`/`note`.

---

## 1. The reframed model — `Forge` owns its batch

### 1.1 One submission, per-action outcomes (unchanged intent, richer payload)

`submit` still reads `local.json`, normalizes, and hands the whole thing to the
forge as one logical review; reconciliation is still per action. What changes is
that **every action can now travel inside the batch**, and the forge decides how
much of it truly lands in a single notification.

```rust
/// The entire review to flush, in forge-neutral terms. One per MR.
pub struct ReviewBatch {
    pub verdict:  Option<Verdict>,     // Approve | RequestChanges | Comment
    pub summary:  Option<String>,      // the review body
    pub actions:  Vec<BatchAction>,    // ordered; ids stable for reconciliation
}

pub enum BatchAction {
    /// A new thread. `anchor` distinguishes line / file / mr placement.
    Comment { key: ActionKey, anchor: Anchor, body: String },
    /// A reply into an existing remote thread.
    Reply   { key: ActionKey, thread: RemoteThreadId, body: String },
    /// Resolve / unresolve an existing remote thread.
    Resolve { key: ActionKey, thread: RemoteThreadId, resolved: bool },
}

/// The forge reports, per action key, whether it is now live on the forge.
/// `Err` still means *nothing* landed (total failure / rolled-back atomic batch);
/// any partial success is `Ok` with a filled-in map.
pub struct BatchOutcome {
    pub landed:     HashMap<ActionKey, bool>,
    pub summary_ok: bool,
    pub verdict_ok: Option<bool>,
    /// How many forge notifications this submission actually produced, so
    /// `submit` can report it (a testable, honest number, not a promise).
    pub notifications: u32,
}
```

`ActionKey` is a stable per-draft index (assigned at normalize time), so
`submit` reconciles by key regardless of how the backend reordered or split the
work — replacing v1's implicit "line/file comments appear in `actions` order,
MR-level ones don't ride the batch" positional coupling (design.md §11), which
was correct-but-fragile.

### 1.2 One anchor type, one inference (folds design.md §5.2 + resolves the §7/§8 critique)

The three near-duplicate placement enums (`Placement`, `RemotePlacement`,
`SubmitPlacement`) and the **two** copies of "action → placement" inference
(`Action::placement` and `submit::to_submit_comment`) collapse into one:

```rust
pub enum Anchor {
    /// A code line (or multi-line span) on a changed file. `end`/`start`
    /// each carry their own `Side` so a span can cross the delete/add boundary.
    Line { path: String, old_path: Option<String>,
           end: LineRef, start: Option<LineRef>, version: DiffVersion },
    /// A whole changed file, no line.
    File { path: String, version: DiffVersion },
    /// The MR conversation, no code anchor.
    Mr,
}
```

`version: DiffVersion` (the `{base, start, head}` a comment was written on) rides
inside the anchor, resolved **once** from the snapshot history at build time.
There is exactly one function `fn anchor_of(action, snapshots, files) -> Anchor`,
used by both the read fold and submit, so the "`file`+`line` ⇒ line, `file` ⇒
file, neither ⇒ mr" rule cannot drift between the two paths. The read view
projects `Anchor` to JSON directly; no `From` chain, no rebuild.

---

## 2. Verified API ground truth

The whole revision rests on these, so they are recorded verbatim (source: the
official GitHub GraphQL / REST and GitLab REST references, 2026-07).

### 2.1 GitHub GraphQL (the review backend)

| Need | Mutation / query | Key fields |
|---|---|---|
| Submit a review (verdict+summary+line threads) | `addPullRequestReview` | `pullRequestId`, `commitOID`, `event` (`COMMENT`/`APPROVE`/`REQUEST_CHANGES`; omit ⇒ **PENDING**), `body`, `threads: [DraftPullRequestReviewThread]` |
| A line thread in that batch | `DraftPullRequestReviewThread` | `body!`, `path`, `line`, `side`, `startLine`, `startSide` — **no `subjectType`** |
| A **file-level** thread | `addPullRequestReviewThread` (on a pending review) | `pullRequestReviewId`, `path`, `subjectType: FILE`, `body!` |
| Publish a pending review | `submitPullRequestReview` | `pullRequestReviewId`, `event`, `body` |
| Reply into a thread | `addPullRequestReviewThreadReply` | `pullRequestReviewThreadId`, `body` |
| Resolve / unresolve | `resolveReviewThread` / `unresolveReviewThread` | `threadId` (`PRRT_…`) → `thread { isResolved }` |
| MR-level (conversation) comment | `addComment` | `subjectId` (PR node id), `body` (an issue comment, **not** part of the review) |
| Read threads | `pullRequest.reviewThreads.nodes` | `id`, `isResolved`, `isOutdated`, `path`, `line`, `startLine`, `startDiffSide`, `comments{ nodes { databaseId, body, author, createdAt } }` |
| Feed / details | `search(type: ISSUE, query: "repo:o/r is:pr …")` → `... on PullRequest` | `number`, `title`, `author{login}`, `baseRefName`, `headRefName`, `headRefOid`, `state`, `isDraft`, `labels`, `updatedAt`, `url` |

Consequence for GitHub submit: a review **with no file comments** is one
`addPullRequestReview`. A review **with file comments** is `addPullRequestReview`
(pending, carrying the line threads + body + commitOID) → one
`addPullRequestReviewThread(subjectType: FILE)` per file comment →
`submitPullRequestReview(event)` — still **one review, one notification**, and it
*can* include file-level comments (which REST cannot). Replies and resolves are
separate mutations, but can be bundled into a single GraphQL document via
aliases, so they cost one HTTP round trip with per-alias outcomes.

### 2.2 GitLab REST (the review backend)

| Need | Endpoint | Key fields |
|---|---|---|
| Draft a diff/file/reply/resolve note | `POST …/merge_requests/:iid/draft_notes` | `note!`; `position{base_sha,start_sha,head_sha,new_path,old_path,new_line/old_line,line_range,position_type}` (`file` since 16.4); `in_reply_to_discussion_id`; `resolve_discussion`; `commit_id` |
| Publish the whole batch | `POST …/draft_notes/bulk_publish` | **`reviewer_state`** (`requested_changes`/`reviewed` — **not** `approved`: the endpoint routes through `UpdateReviewerStateService`, which sets a review state, never a formal approval), **`note`** (summary body), `internal` — all optional; publishes *all* of the user's pending drafts on the MR as one review |
| Delete a draft (rollback) | `DELETE …/draft_notes/:id` | — |
| Read discussions | `GET …/merge_requests/:iid/discussions` | notes with `position`, `resolvable`, `resolved`, `system` |

Consequence for GitLab submit: **one** `bulk_publish` publishes line comments,
file comments, replies (`in_reply_to_discussion_id`), the summary (`note`), and
the reviewer state (`reviewer_state`) together — one notification.
`RequestChanges → "requested_changes"`, `Comment → "reviewed"`. **`Approve` is
the exception:** `reviewer_state: "approved"` routes through
`UpdateReviewerStateService` and records only a *review state*, not a formal
approval (`ApprovalService`), so an approve verdict is a *separate*
`POST …/approve` after the publish — never folded in, or the MR would silently
not be approved. MR-level conversation comments are position-less draft notes, so
they ride the batch; a bare resolve is a separate PUT (a draft note needs a body).

**Version target.** `reviewer_state`/`note` on `bulk_publish` and the boolean
`draft` list filter all landed by GitLab 19.0, which is our floor. There is no
version probe and no fallback path — we assume ≥ 19, keeping the backend a single
clean mapping instead of a forked one. (Personal tooling; bumping the floor is
free.)

---

## 3. GitHub GraphQL backend

### 3.1 Transport

A thin `graphql(query, variables) -> Value` helper beside the existing REST
`request`, same `ureq` client, same `Auth::Bearer`, same retry/backoff. One POST
to `{api_base}/graphql` (`https://api.github.com/graphql`, or
`https://<host>/api/graphql` for Enterprise). Errors are read from the
`errors[]` array, not just HTTP status (GraphQL returns `200` with partial data +
errors), so `graphql` surfaces both.

Node ids: a review targets a PR by number, but the mutations want the PR **node
id** (`PR_…`) and thread node ids (`PRRT_…`). One `repository.pullRequest(number)`
query resolves the PR node id and, in the same call, everything `mr_details`
needs. Thread node ids come from `reviewThreads` (so `list_threads` already has
them for reply/resolve).

### 3.2 The review half, mapped

- **`list_threads`** — one `reviewThreads` page-walk. Native grouping; `resolved`
  ← `isResolved`; `outdated` ← `isOutdated` *(kept as a fallback signal; the
  primary is local, §5)*; comments carry `databaseId` for the `local.json`
  `remote:<id>` form. This alone fixes v1's "resolved always false" and the
  `in_reply_to_id` fragility.
- **`mr_details` / feed** — `search(type: ISSUE, "repo:o/r is:pr …")` returning
  `PullRequest` nodes. Real `baseRefName`/`headRefName`/`headRefOid` → stack
  links and head SHA restored for feed-fetched PRs (§6). `is:pr` guarantees PRs,
  not issues.
- **`submit`** — the pending-review flow in §2.1. Returns a `BatchOutcome`
  keyed by `ActionKey`. Because `addPullRequestReview` is atomic, a create
  failure ⇒ the review keys fail together. Once the pending review exists, each
  `addPullRequestReviewThread`/reply/resolve reports its own key outcome.
- **`resolve`** — now supported (`resolveReviewThread`/`unresolveReviewThread`),
  closing the v1 GitHub gap.
- **`permalink`** — unchanged (blob URL); paths get URL-encoded (a v1 cosmetic
  bug, §7).

### 3.3 Scope: the whole GitHub forge moves to GraphQL (decided, O1)

**Decision (O1): the entire GitHub forge — review *and* `stack` half — moves to
GraphQL, one transport.** `find`/`create`/`set_base`/`set_body`/
`apply_attributes` are re-expressed as GraphQL queries/mutations
(`repository.pullRequests`/`search`, `createPullRequest`, `updatePullRequest`,
`addLabelsToLabelable`/`requestReviews`, …).

This is the largest single change and it **touches `wits stack`**, which is a
shared, tested command. It is therefore its own sequencing step (§9) with its own
guard: `wits stack`'s existing behaviour must not regress (its tests stay green,
and a dry-run of a real stack push produces the same plan). REST is retained only
for the object-fetch refspec (`git fetch refs/pull/<n>/head`), which is a git
operation, not an API call.

---

## 4. GitLab backend — one `bulk_publish`

`submit` on GitLab is:

1. **Draft every action** as a draft note, in bounded-parallel POSTs, recording
   each draft's id under its `ActionKey`:
   - line/file comment → `position` (per-comment `version`, cross-snapshot intact);
   - MR-level comment → position-less note;
   - reply → `in_reply_to_discussion_id`;
   - resolve → `resolve_discussion: true` on a reply-less draft (or on the reply
     that resolves it).
2. **`bulk_publish`** with `note` = summary and `reviewer_state` = the verdict
   *when it is `request-changes`/`comment`*. One atomic publish, one notification.
   An `approve` verdict is **not** a `reviewer_state` (that records only a review
   state, not a real approval) — it is a separate `POST …/approve` after the
   publish. A bare resolve is a separate PUT.
3. **Reconcile.** Any draft POST that failed ⇒ roll back the drafts that landed
   (`DELETE`) and keep the whole batch local for a clean retry — the v1
   all-or-nothing-per-attempt discipline (design.md §11) is retained, now
   covering the *entire* review, not just line comments.

No old-instance fallback: GitLab ≥ 19 is assumed (§2.2), so there is no version
probe. This deletes the summary-as-lone-draft dance and the RequestChanges-as-
unapprove hack, leaving `approve` as the one deliberately-separate verdict call.

---

## 5. Outdated — computed locally, uniformly

The requirement ("回顧 outdate comment, 允許 outdated comment 被 push") is met by
making outdated a **local inference**, not a per-forge field we cannot trust
equally (GitLab exposes none; GitHub only via GraphQL):

> A thread is **outdated** iff the line it is anchored to differs between the
> commit it was written on (`anchor.version.head_sha` / the thread's `commit`)
> and the current snapshot head — determined from the objects we already pin,
> `git diff <anchor_commit>..<current_head> -- <path>` intersecting the anchored
> line on its side. A `File`/`Mr` thread is never outdated; a thread whose file
> was untouched between the two commits is trivially current.

Properties:

- **Uniform** across GitHub and GitLab — identical behaviour, identical tests.
- **Offline & testable** — a pure function over two commits + an anchor + a diff;
  exactly the fuzz/test surface the owner wants. No network.
- **Consistent with the philosophy** — we own coordinates; the editor renders.

Fallbacks: if the anchor commit's objects are not local (a thread on a commit we
never fetched), fall back to the forge's own flag (`isOutdated` on GitHub; `false`
on GitLab) and note the degrade. `fetch` can also pin such commits opportunistically.

---

## 6. Feed — one query per forge, real MR objects

- **GitHub** — `search(type: ISSUE, "repo:o/r is:pr is:open …")` over GraphQL,
  reading `PullRequest` nodes. Rich filters (label/author/assignee/reviewer, via
  the same qualifiers) **and** `baseRefName`/`headRefName`/`headRefOid` in one
  query. The v1 REST `search/issues` (issue shells, no head) is retired.
- **GitLab** — unchanged in spirit (`GET …/merge_requests` with server-side
  filters), which already returns base/source/`sha`.

The `limit` overshoot (design.md §9's "hard cap" that wasn't) is made real by
truncating the final page to `limit`. Both forges then agree: feed items carry
enough to link a stack and to `checkout --next/--prev` without a full `fetch`.

---

## 7. Smaller corrections folded in

- **Worktree store root** — resolve the default via `--git-common-dir`, not
  `--absolute-git-dir`, so running `review` from inside a `checkout` worktree
  finds the same store. (Pins already live in the common ref store.)
- **Permalink path encoding** — URL-encode path segments in both backends.
- **`User-Agent`** — one honest `wits/<version>` for every forge call (`stack`
  and `review` share the transport); the split `wits-stack`/`wits-review` bought
  nothing.
- **GitHub outdated-thread line** — a `reviewThread`'s `line`/`startLine` go
  `null` once the thread is outdated; read `originalLine`/`originalStartLine`
  (which pair with `originalCommit`, the thread's anchor commit) so an outdated
  thread keeps a real line instead of `0`.
- **`iso_date_to_epoch_day`** — reject impossible days (`02-31`).
- **Pre-submit local validation (optional, opt-in)** — since the diff and thread
  list are local, `submit`/`draft` can warn on a comment whose file/line is not
  in the diff, or a reply/resolve to an unknown thread, before the network. Also
  surface an orphan pending reply/resolve in `show` rather than dropping it
  silently. Serves the "easy to debug" goal.

---

## 8. The honest capability matrix (revised)

| Concern | GitHub (GraphQL) | GitLab (REST) |
|---|---|---|
| Batch: verdict + summary + line comments | one `addPullRequestReview` → one notification | one `bulk_publish` → one notification |
| Batch: **file-level** comments | yes, via pending review + `addPullRequestReviewThread(FILE)` + submit (still one review) | yes, `position_type:file` draft note |
| Batch: **MR-level** comments | **no** — `addComment` is a separate issue comment | yes, position-less draft note |
| Batch: replies / resolves | separate mutations, one HTTP doc via aliases | yes, ride the draft batch (`in_reply_to`/`resolve_discussion`) |
| `request-changes` / `comment` verdict | native (`REQUEST_CHANGES`/`COMMENT`) | **native** (`reviewer_state:"requested_changes"`/`"reviewed"`) |
| `approve` verdict | part of the review | **separate `POST …/approve`** (`bulk_publish`'s `"approved"` sets only a review state, not a real approval) |
| Thread resolve/unresolve | **supported** (`resolveReviewThread`) | supported (`resolve_discussion` / discussion PUT) |
| `resolved` read-back | `isResolved` | notes' `resolved`/`resolvable` |
| `outdated` | **local** (forge `isOutdated` as fallback) | **local** (no forge fallback) |
| Cross-snapshot per-comment anchor | one `commitOID` per review ⇒ batch anchors to one snapshot | per-note `position` ⇒ true per-comment |
| Feed returns real MR (base/head) | yes (GraphQL `search`) | yes (MR list) |

Two honest asymmetries remain, now *inherent* rather than self-inflicted:
GitHub can't fold an MR-level comment into the review batch, and GitHub still
anchors a whole review to one `commitOID` (so cross-snapshot drafting stays
GitLab-only). Both are real API limits, documented, not papered over.

---

## 9. Impact, sequencing, and open decisions

**Blast radius.** `forge/review.rs` (types: `ReviewBatch`/`BatchAction`/
`BatchOutcome`/`Anchor`), `forge/github.rs` (new GraphQL review half + a
`graphql` transport helper), `forge/gitlab.rs` (submit rewrite + fallback),
`cmd/review/model.rs` (collapse placements to `Anchor`, single inference),
`cmd/review/submit.rs` (key-based reconciliation), `cmd/review/fetch.rs` +
`show.rs` (local outdated), `store.rs` (git-common-dir), `config`/feed (limit
cap). Docs: fold this into `design.md`, refresh `review.md`/`json.md`/`store.md`,
update `README.md`. **No backward-compat constraint** (personal tooling), so the
schema can change freely and stays `1` — stored shapes change without a bump.

**Suggested order (worst-first, each independently testable):**
1. `Anchor` consolidation + single inference (pure refactor, unblocks the rest).
2. `ReviewBatch`/`BatchOutcome` + key-based reconciliation (forge-neutral).
3. GitLab `bulk_publish` rewrite + fallback.
4. GitHub GraphQL **review** half (transport, threads, submit, resolve, feed).
5. GitHub GraphQL **stack** half (O1) — separate, guarded against `stack`
   regression; can trail the review half.
6. Local outdated.
7. Fixture-based unit tests for both mappers (the highest-ROI safety net).
8. Smaller corrections (§7).

**Decisions taken:** O1 → whole GitHub forge to GraphQL (§3.3). O2 → outdated is
**local-primary**, forge flag only as fallback when the anchor commit's objects
are absent (§5).

**Open decisions still to settle:**

- **O3 — GitLab version gate. Resolved: dropped.** We target GitLab ≥ 19 and use
  the `bulk_publish` `reviewer_state`/`note` params and the boolean `draft` list
  filter unconditionally — no `GET /version` probe, no fallback path.
- **O4 — MR-level comment on GitHub.** Accept the extra notification (it's a
  genuine API limit), or drop MR-level comments from a GitHub *review* batch and
  document them as always-separate?
- **O5 — Pre-submit validation.** In scope now (§7), or deferred?
