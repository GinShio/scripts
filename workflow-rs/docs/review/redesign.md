# `wits review` — API-native revision

> Status: **implemented.** This document is the rationale for the revision that
> reshaped the *forge boundary*: how a review is submitted and read back, grounded
> in the **current** GitHub and GitLab APIs rather than a lowest-common-denominator
> shape smeared over both. The spine of [`design.md`](design.md) (forge-first
> acquisition, snapshot pinning, the three-file store, the `--json`/`local.json`
> contracts, the local/network split) is unchanged; `design.md`'s §6/§10/§11/§17
> have been brought in step with what this revision delivered.
>
> API claims here were re-checked against the vendor documentation *and release
> status* in 2026-07 (the exact fields are in §2). One earlier claim did not
> survive that check and has been corrected throughout: the `bulk_publish`
> `reviewer_state`/`note` body is the **unmerged** gitlab-org/gitlab!237813, in no
> shipped release — so the summary rides as a draft note and the verdict is a
> separate `approve`/`unapprove`/no-op call (§2.2, §4). What still cannot be
> exercised in-tree is a *live* call to either forge (the test suite is offline by
> design): the mapping code is pinned by fixture unit tests, but the end-to-end
> network paths — GitHub GraphQL mutations, GitLab draft-note `line_code` and the
> bodyless `bulk_publish` — still want a one-time live smoke test.

---

## 0. Why revise

The v1 forge layer picked one intermediate shape — *"a batch is a verdict +
summary + line/file comments; everything else is a separate call"* — and bent
each backend to fit. Confronting it with the real APIs shows the shape matches
**neither** platform:

- **GitLab can batch far more than that.** Its draft-notes + `bulk_publish`
  primitive natively carries line comments, file comments, MR-level notes,
  replies, thread resolutions, the summary body, **and** the verdict — the whole
  review, one atomic publish, one notification for the comments, replies and
  summary. The one caveat, discovered when the code was checked against the
  *released* API (below): the `reviewer_state`/`note` body params on
  `bulk_publish` are an **unmerged proposal** (gitlab-org/gitlab!237813) and ship
  in no release, so the verdict cannot ride the publish and the summary rides as
  a position-less draft note instead. The `requested_changes` reviewer *state*
  exists, but there is no released API to set it — `request-changes` therefore
  maps to `unapprove` (its concrete released effect), not to a phantom param.

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
2. **GitLab submit becomes one `bulk_publish`** for the comments/replies/summary,
   with the verdict as a separate released `approve`/`unapprove`/no-op call — the
   `reviewer_state`/`note` body that would fold it in is unreleased (§2.2).

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

The near-duplicate placement types and the two copies of the "action →
placement" inference collapse into one `Anchor`, serialized directly as the
read-view shape (no separate "placement" mirror):

```rust
#[derive(Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum Anchor {
    /// A code line (or multi-line span) on a changed file. `end`/`start`
    /// each carry their own `Side` so a span can cross the delete/add boundary.
    Line { path: String, old_path: Option<String>,
           end: LineRef, start: Option<LineRef> },
    /// A whole changed file, no line.
    File { path: String },
}
// The MR-level conversation is `Option<Anchor>::None` — no `Mr` variant needed.
```

The snapshot version a comment was written on (`DiffVersion {base, start, head}`)
does **not** live inside the anchor: it rides on the thread (`commit`) and on the
`BatchAction`, resolved **once** from the snapshot history at build time. There is
one inference, `comment_anchor(...)`, shared by the read fold and submit, so the
"`file`+`line` ⇒ line, `file` ⇒ file, neither ⇒ MR-level" rule can't drift
between the two paths. The read view serializes `Anchor` directly; no `From`
chain, no rebuild.

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

Consequence for GitHub submit: a review with **only line comments/summary/
verdict** is one atomic `addPullRequestReview`. A review that also has **file
comments or replies** uses the pending flow — `addPullRequestReview` (pending,
carrying line threads + body + commitOID) → one
`addPullRequestReviewThread(subjectType: FILE)` per file comment → one
`addPullRequestReviewThreadReply(pullRequestReviewId: <pending>, …)` per reply →
`submitPullRequestReview(event)`. Still **one review, one notification**, now
including file-level comments (which REST cannot) **and replies** (which join the
pending review by id, exactly as the web UI batches them). Resolves are separate
mutations but do not notify; an MR-level conversation comment is the one
unavoidable separate notification (`addComment`).

### 2.2 GitLab REST (the review backend)

| Need | Endpoint | Key fields |
|---|---|---|
| Draft a diff/file/reply/resolve/summary note | `POST …/merge_requests/:iid/draft_notes` | `note!`; `position{base_sha,start_sha,head_sha,new_path,old_path,new_line/old_line,line_range,position_type}` (`file` since 16.4; a `line_range` endpoint **requires** `line_code` = `SHA1(path)_old_new`); `in_reply_to_discussion_id`; `resolve_discussion`; `commit_id`. A note with no `position` is an MR-level/summary note. |
| Publish the whole batch | `POST …/draft_notes/bulk_publish` | **no body** in any shipped release — publishes *all* of the user's pending drafts on the MR as one review. (A `reviewer_state`/`note` body is the *unmerged* proposal !237813, absent from every release and silently ignored by Grape if sent.) |
| Set the verdict | `POST …/approve` / `POST …/unapprove` | the only released reviewer actions; there is **no** released API to set the `reviewed`/`requested_changes` reviewer *state* |
| Delete a draft (deferred cleanup) | `DELETE …/draft_notes/:id` (404 ⇒ already gone) | — |
| Read discussions | `GET …/merge_requests/:iid/discussions` | notes with `position`, `resolvable`, `resolved`, `system` |

Consequence for GitLab submit: **one** `bulk_publish` (no body) publishes line
comments, file comments, replies (`in_reply_to_discussion_id`) **and the summary**
(a position-less draft note) together — one notification. The verdict is a
*separate* released call: `Approve → POST …/approve`;
`RequestChanges → POST …/unapprove` (the released effect of requesting changes —
the reviewer's approval is removed — since the formal `requested_changes` state
has no released API); `Comment → no-op` (leaving notes no longer auto-sets
`reviewed`, and nothing released sets it). MR-level conversation comments are
position-less draft notes, so they ride the batch; a bare resolve is a separate
PUT (a draft note needs a body).

**Version target.** The boolean `draft` list filter is released (GitLab ≥ 16) and
the feed relies on it. The `reviewer_state`/`note` `bulk_publish` params are
**not** released (!237813 is unmerged), so nothing depends on them: the summary
rides as a draft note and the verdict is a separate `approve`/`unapprove`. If
!237813 ships, folding the verdict + summary back into the one publish is a
localized optimization behind a version probe — not a correctness dependency.
(Personal tooling; revisit when it lands.)

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

0. **Pre-flight cleanup.** `DELETE` the draft-note ids a prior failed attempt
   recorded (`ReviewBatch.stale`), treating `404` as already-gone. A *real*
   delete failure aborts here, before any POST — an undeleted orphan would be
   swept into this run's `bulk_publish` and duplicated.
1. **Draft every action** as a draft note, in bounded-parallel POSTs, recording
   each draft's id:
   - line/file comment → `position` (per-comment `version`, cross-snapshot intact;
     a multi-line `line_range` carries the required `line_code`);
   - MR-level comment → position-less note;
   - reply → `in_reply_to_discussion_id`;
   - **summary → position-less draft note** (the released `bulk_publish` has no
     `note` param), so it publishes with the batch.
   (Resolves are not draft notes — see §8 — so they are separate PUTs.)
2. **`bulk_publish` (no body)** — one atomic publish of all pending drafts, one
   notification. The verdict is a *separate* released call afterwards:
   `approve → POST …/approve`, `request-changes → POST …/unapprove` (its released
   effect; the formal `requested_changes` state has no API), `comment → no-op`.
   No `reviewer_state`/`note` body is sent (that is the unmerged !237813).
3. **Reconcile — deferred cleanup, not this-attempt rollback.** A draft-note POST
   failure, or a `bulk_publish` failure, records the posted-but-unpublished ids in
   `BatchOutcome.inflight` and keeps the whole batch local; it does **not** delete
   now. `submit` persists those ids and step 0 of the *next* attempt deletes them.
   Deferring makes cleanup idempotent (retried until it succeeds) and removes the
   double-failure orphan the old this-attempt `DELETE` could leave behind.

The summary-as-draft-note is *not* a fallback dance but the only released path;
`approve`/`unapprove`/no-op are the released verdict actions. If !237813 ships,
folding the verdict + summary into the one publish becomes a version-gated
optimization, never a correctness dependency.

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
| Batch: replies | **ride the review** (`addPullRequestReviewThreadReply` with the pending review's id) — one notification | ride the draft batch (`in_reply_to`) |
| Batch: resolves | separate mutations (quiet, no notification) | ride the draft batch (`resolve_discussion`) |
| `request-changes` verdict | native (`REQUEST_CHANGES`) | `POST …/unapprove` (no released API for the formal `requested_changes` state; unapprove is its released effect) |
| `comment` verdict | native (`COMMENT`) | no-op (leaving notes no longer auto-sets `reviewed`; nothing released sets it) |
| `approve` verdict | part of the review | **separate `POST …/approve`** |
| summary body | rides the review (`body`) | position-less draft note (released `bulk_publish` has no `note` param) |
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

- **O3 — GitLab version gate. Resolved: no gate needed.** The submit path uses
  only *released* endpoints (bodyless `bulk_publish`, `approve`/`unapprove`,
  draft notes with `line_code`) and the released boolean `draft` list filter, so
  there is nothing to probe. The earlier plan to send `reviewer_state`/`note` on
  `bulk_publish` was dropped once that proved to be the unmerged !237813 (absent
  from every release, silently ignored if sent — it would have dropped the
  summary and verdict while reporting success). Revisit as an optimization if it
  ships.
- **O4 — MR-level comment on GitHub.** Accept the extra notification (it's a
  genuine API limit), or drop MR-level comments from a GitHub *review* batch and
  document them as always-separate?
- **O5 — Pre-submit validation.** In scope now (§7), or deferred?
