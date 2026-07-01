# Git hooks — Guide

A shared set of git hooks that run in every repository, do the routine chores —
formatting, linting, catching secrets and stray conflict markers, keeping LFS
and per-branch bookkeeping in step — and otherwise stay out of your way. This is
how to turn it on, what each hook is for and how to tune it, and how to switch
any of it off. For *why* it is built this way, see the [design notes](design.md).

## Turning it on

Point git at this directory, once per machine:

```sh
git config --global core.hooksPath /path/to/scripts/git/hooks
```

A couple of orientation points before the details. Most of this is automatic and
silent; you mainly notice it when a `pre-commit` or `pre-push` check turns you
away, which is the point. Each configurable behaviour is described with its keys
below; how those keys are read — git config versus environment — is spelled out
once in [How settings are read](#how-settings-are-read).

Two external tools also ride along on several hooks and need no setup from you:
**Git LFS** keeps large-file objects synced (on checkout, merge, commit, and
push), and **git-branchless** records history events for its smartlog/undo (on
nearly every hook). Both are silent when the tool isn't installed and neither has
anything to configure, so the sections below only cover the framework's own
scripts.

## How settings are read

Every knob in this guide is a git config key under the `hooks.ginshio.`
namespace, and every one has an environment-variable twin. They cover different
needs:

- **git config** is the standing choice — `git config --local <key> <value>` for
  the current repo, `--global` for all of them. Reach for it when you want a
  lasting preference.
- **The environment variable** is a one-off for the command in front of you, and
  it overrides config when both are set — the "not this time" escape hatch.

The environment name is mechanical: drop the `hooks.ginshio.` namespace,
upper-case what remains, and turn every `-` and `.` into `_`, yielding a
`GINSHIO_HOOKS_…` name.

```
hooks.ginshio.pre-commit.code-formatter-disable  →  GINSHIO_HOOKS_PRE_COMMIT_CODE_FORMATTER_DISABLE
hooks.ginshio.pre-commit.clang-format-enabled    →  GINSHIO_HOOKS_PRE_COMMIT_CLANG_FORMAT_ENABLED
```

The one exception is the global kill switch: `hooks.ginshio.disable` also answers
to `GINSHIO_HOOKS_DISABLE_ALL`. Boolean keys accept the usual git spellings —
`true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off`.

Throughout the sections below keys are written relative to their hook: a setting
shown as `clang-format-enabled` under `pre-commit` is the full key
`hooks.ginshio.pre-commit.clang-format-enabled`.

## `pre-commit`

Fires after you stage changes and before the commit is recorded. It is the
busiest hook here and the only one that can reject a commit — a failure stops the
commit with a note on what to fix, so problems surface now instead of in review.
To bypass the whole hook for one commit, `git commit --no-verify`.

### Formatter

Keeps the tree consistently formatted without you having to think about it, so
what you commit is already clean. C/C++ go through `clang-format`, Rust through
`rustfmt`, Zig through `zig fmt`, Python through `ruff` (falling back to `black`
+ `isort`), and any other text file gets a trim of trailing whitespace and a
guaranteed final newline. A language is handled only when its formatter is on
your `PATH`, and each part is independent, so you can bow out where it fights a
project's own conventions.

It formats the **staged content**, not the working tree: it rewrites the version
in the index and, when your working copy has no unstaged edits, updates that too.
A partially-staged file therefore keeps its unstaged changes intact — the commit
gets the formatted version, your in-progress edits are left alone.

- `clang-format-enabled`, `rust-fmt-enabled`, `zig-fmt-enabled`,
  `python-fmt-enabled` — one per language, on by default.
- `whitespace-enabled` — the generic trim/final-newline pass, on by default.
- `clang-format-style` — a named style (`llvm`, `google`, …) for C/C++ when the
  repo has no `.clang-format` of its own.

### Linter

Catches mistakes cheaply, before they reach a reviewer. It runs the fast
analyzers over what you're committing — `ruff` (or `flake8`) for Python,
`zig ast-check` for Zig, `cargo clippy` (as `-D warnings`) for Rust — and stops
the commit on a finding. Like the formatter it is per-language and only runs
where the tool exists.

- `python-lint-enabled`, `zig-lint-enabled`, `rust-lint-enabled` — on by default.

### Sanity checks

A safety net for the mistakes that are easy to make and tedious to undo. It
refuses a commit that still carries an unresolved merge-conflict marker, one that
adds a symlink pointing nowhere, or one that would drag in an oversized file —
usually a fat-fingered `git add` of a build artifact or a dataset.

- `sanity-checks-max-file-size` — the size ceiling in bytes (default 25 MiB).

### Marker guard

Lets you plant a tripwire in your own code. Stage a line containing
`DO_NOT_SUBMIT`, `NOCOMMIT`, or `FIXME_BLOCKER` and the commit is refused until
you remove it — the reliable way to make sure a debug hack or a note-to-self
never ships. Nothing to configure.

### Secret scan

A last line against committing credentials. It scans the staged diff with
`gitleaks` (preferred) or `git-secrets` and blocks on a match. There is no switch
to flip: it is active whenever one of those scanners is on your `PATH`, so
installing the tool is how you turn it on. A genuine false positive can be waved
through with `git commit --no-verify`.

### Encoding check

Keeps staged text honest: LF newlines only, and valid UTF-8. A staged text file
carrying a CR/CRLF line ending or an invalid UTF-8 byte is rejected. Binary blobs
are skipped, and so is the UTF-8 half when `iconv` isn't available. Nothing to
configure — for automatic newline normalization on top of this, let git handle it
with a `.gitattributes` `text=auto eol=lf`.

### Protected-branch prompt

A guard against committing straight onto a shared branch by accident. Enabled, it
asks you to confirm before committing while `master`, `dev`, `release-*`, or
`patch-*` is checked out. Off by default, since whether a direct commit is a
mistake depends entirely on how you work.

- `warn-protected-enabled` — off by default; set true to get the prompt.

## `prepare-commit-msg`

Runs as git assembles the initial commit message, before your editor opens, which
is the moment to seed it with something.

### Issue-ID prefix

Saves retyping a ticket number into every commit. Enabled, it reads an issue ID
out of the current branch name and prepends it to a *new* message — so on
`feature/PROJ-123-login` your message opens with `[PROJ-123] ` already there. It
only touches fresh messages (never an amend, merge, or squash) and won't
double-prefix on a re-edit. Off by default so it never intrudes on repos that
don't work this way.

- `issue-tracker-enabled` — off by default.
- `issue-tracker-regex` — what to pull from the branch name (default
  `[A-Z]+-[0-9]+`).
- `issue-tracker-format` — how to wrap the ID, `printf`-style with `%s` as the ID
  (default `[%s] `).
- `issue-tracker-default` — a fallback ID to use when the branch name carries
  none.

## `pre-push`

Runs before git hands refs to a remote, and can abort the push.

### Protected-branch prompt

The push-side counterpart to the commit prompt: enabled, it asks for confirmation
before pushing to a protected branch on the remote. Off by default.

- `warn-protected-enabled` — off by default; set true to get the prompt.

## `post-checkout`

Runs after `git checkout`/`switch` and after `clone`. Everything here is advisory
— it never blocks the operation, it just keeps your working environment in step
with the branch you moved to.

### Workspace restore

Keeps your editor pointed at the right build as you jump between branches. For an
out-of-tree build it repoints the working tree's `compile_commands.json` at the
active branch's build directory, so language servers and clang-tooling index the
branch you're actually on. It only manages that path when it is a symlink or
absent, and never clobbers a real file you keep in-tree. Nothing to configure.

### Dependency-change warning

A nudge so you don't run against stale dependencies. When a checkout changes a
lockfile — `package-lock.json`, `Cargo.lock`, `go.sum`, `poetry.lock`, and the
rest — it reminds you to reinstall. It only ever warns; it won't run your package
manager for you. (The same script also runs on `post-merge`.)

### Maintenance enrolment

Opts each repo into git's own upkeep. The first time you land in a repo it
registers it with `git maintenance start`, so git's background tasks
(commit-graph, gc, and so on) keep the repo fast without you scheduling anything.
Nothing to configure.

### Branchless bootstrap

Sets up `git-branchless` for a repo the first time you check out a branch there —
typically right after cloning — when branchless is installed and not already
initialized, so a fresh clone is ready without a manual `git branchless init`.

## `reference-transaction`

Fires whenever refs change. The scripts here react to one case in particular —
branch deletion — and act only once the change is actually committed, so an
aborted rebase or a rolled-back transaction never triggers them.

### Machete cleanup

Keeps your `git-machete`/stack layout honest as branches come and go. Delete a
branch and it is removed from `.git/machete` with its children spliced up to its
parent, so the tree stays valid instead of collecting dangling entries you'd have
to prune by hand. Runs wherever a machete file exists; nothing to configure.

### Build-directory cleanup

Reclaims disk when you delete a branch. Opt in, and deleting a branch also removes
its build directory (resolved through the project's `builder.py`). Off by default
because it deletes files — enable it per repo where you want the housekeeping.

- `cleanup-build-dir-enabled` — off by default; set true to remove build
  directories on branch deletion.

## The recorder hooks

`post-commit`, `post-merge`, `post-applypatch`, `post-rewrite`, and `pre-auto-gc`
mostly exist to feed the two pass-through integrations — `git-branchless` on all
of them, Git LFS on `post-commit` and `post-merge`. `post-merge` additionally
reuses the dependency-change warning above. There is nothing framework-specific
to configure on any of them.

## Turning pieces off

Beyond the per-behaviour switches above, you can silence things wholesale — when
a hook is wrong for a repo, or simply in your way this once. Three scopes:

- **Everything** — `hooks.ginshio.disable`
- **One hook** — `hooks.ginshio.<hook>.disable`, e.g. `hooks.ginshio.pre-commit.disable`
- **One script** — `hooks.ginshio.<hook>.<script>-disable`, naming the script
  without its numeric prefix, e.g. `hooks.ginshio.pre-commit.code-formatter-disable`

As everywhere, config is the standing choice and the environment variable is the
one-shot that wins for a single command (see [How settings are
read](#how-settings-are-read)):

```sh
git config hooks.ginshio.pre-commit.disable true      # this repo, from now on
GINSHIO_HOOKS_PRE_COMMIT_DISABLE=1 git commit …        # only this commit
```

## Bringing your own hooks

If a repo already carries hooks — a Husky setup, a `.githooks` directory, a lone
`scripts/lint.sh` — you needn't choose between them and this framework; they run
in the same pass, under the same on/off rules, and *after* the built-in scripts,
so a project's own checks get the last word. Name the directories to scan (Husky's
single-file `pre-commit` and split `pre-commit.d/` forms are both understood), or
map individual scripts to a hook. Entries are colon-separated, absolute or
relative to the repo root:

```sh
git config hooks.ginshio.external-dirs ".husky:.githooks"
git config hooks.ginshio.pre-commit.external-scripts "scripts/lint.sh:tools/check-fmt"
```

## Seeing what a hook is doing

When something surprises you, turn up the logging for the next command — the
levels run from silent to a full shell trace:

```sh
GINSHIO_HOOKS_LOG_LEVEL=3 git commit …   # 0 silent · 1 errors · 2 warnings (default) · 3 info · 4 trace
```
