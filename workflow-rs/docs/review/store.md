# `wits review` — the store and how to move it

`wits review` keeps its state in JSON files and a few git refs. This is the
reference for that layout: what lives where, and how to carry an in-progress
review to another machine.

## Where the store lives

The base directory is resolved on a three-rung ladder, first hit wins:

| Rung | Path |
|---|---|
| 1 | `$WITS_REVIEW_DIR` — an explicit override. |
| 2 | `$XDG_STATE_HOME/wits/review` — when `XDG_STATE_HOME` is set. |
| 3 | `$GIT_DIR/wits/review` — the default, per-clone, beside `.git/machete`. |

State (this store, `$XDG_STATE_HOME`) is kept separate from config (the feed
`review.toml`, `$XDG_CONFIG_HOME`). Under the base, each repo has its own subtree
keyed by the target remote's identity, and each MR has its own directory:

```
<base>/<host>/<owner>/<repo>/
├── <id>/
│   ├── info.json       # the MR's metadata + diff state
│   ├── comments.json   # the forge's discussion (a cache)
│   └── local.json      # your unsubmitted review (only present while drafting)
├── <id>/ …
└── current             # the MR the last `checkout` materialized
```

For a GitLab nested group the `<owner>` segment contains slashes and becomes
nested directories — that is fine.

## The three files

### `info.json` — metadata + diff state

The MR's necessary information: the inbox row and the header/coordinates of the
detail view. Refetchable; safe to hand-tweak.

| Field | Meaning |
|---|---|
| `schema` | Store version. |
| `mr` | The MR object (id, display, state, draft, title, author, base, source, head_sha, updated_at, labels, web_url — see [json.md](json.md#mr-object)). |
| `version` | The diff SHAs a comment anchors against: `base_sha`, `start_sha`, `head_sha` (start/base coincide on GitHub; GitLab uses all three). |
| `fetched_at` | Unix timestamp of the last fetch (used by `prune --older-than`). |
| `commits` | Commits in `base..head`, derived **locally** from the fetched objects. |
| `files` | Files the MR touched, derived locally. |

A **feed** fetch fills only `mr`, leaving `version`/`commits`/`files` empty; a
full `fetch <mr>` fills them.

### `comments.json` — the forge's discussion (a cache)

`{ "schema": 1, "threads": [ … ] }`, where each thread has the shape in
[json.md](json.md#thread-object). This is a pure cache: overwrite or delete it
freely and refetch. Everyone else's comments live here.

### `local.json` — your unsubmitted review (the file you edit)

The one file you write, defined in
[json.md](json.md#localjson---the-write-contract): an optional `verdict` and
`summary`, and an append-style `actions` list. It exists only while you have a
draft — `submit` deletes it once flushed, and an empty draft is the same as no
file. This is the state that would be *lost*, so it is what migration moves.

## Git refs — pinning reviewed objects

Fetching an MR pulls its objects and holds them alive with the tool's own refs,
so a later force-push on the author's side can't garbage-collect the snapshot you
reviewed:

| Ref | Points at |
|---|---|
| `refs/wits/review/<mr>/<snapshot-sha>` | the reviewed head commit |
| `refs/wits/review/<mr>/<snapshot-sha>-base` | its base, when not an ancestor of head |

The names carry only what disambiguates within one clone — the MR number and the
SHA. Enumerate them with `git for-each-ref refs/wits/review/`; `prune` deletes
them (letting git collect the objects) once an MR is terminal or dormant.

## Moving a review to another machine

"Sharing" here means carrying *your own* in-progress review between *your own*
machines — the forge is the collaboration layer, not this store. Because
`info.json`/`comments.json` are refetchable, only `local.json` needs to travel:

```sh
# on the first machine — copy the drafts you care about
base=$(git rev-parse --absolute-git-dir)/wits/review/github.com/me/proj
cp "$base/123/local.json" /media/key/mr123-local.json

# on the second machine
base=$(git rev-parse --absolute-git-dir)/wits/review/github.com/me/proj
mkdir -p "$base/123" && cp /media/key/mr123-local.json "$base/123/local.json"
wits review fetch 123        # rebuild info/comments and pin the objects
wits review show 123         # your pending review is back, merged in
```

`local.json` refers to threads and lines on the MR's current snapshot, so the
second machine must be able to `fetch` the same MR — it can, the MR still exists
on the forge. If you point `WITS_REVIEW_DIR` at synced storage, both machines
share the drafts automatically and only the per-clone git refs are rebuilt by
`fetch`.

## Schema versioning

Every file and every `--json` payload carries an integer `schema` (currently
`1`); an incompatible shape change bumps it. Because `info.json`/`comments.json`
are disposable, a bump can be handled for them by refetching; only `local.json`
migrations (if ever needed) warrant more care.
