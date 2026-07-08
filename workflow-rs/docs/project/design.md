# `wf project` — Design

> Status: **implemented (v1)**. This is the agreed shape of the `project` tool
> and the reasoning behind it. Items marked **[open]** still need a decision;
> everything marked **future**/**TODO** is deliberately out of v1; everything else
> is settled and reflected in the code.
>
> This file explains *why the tool is shaped the way it is*. The reader-facing
> documents explain *how to drive it*: [`project.md`](../project.md) is the usage
> guide and [`project/reference.md`](reference.md) is the exhaustive key/flag
> reference. Neither restates the other — rationale lives here, behaviour-for-users
> lives there.

This is the reference design for the `project` sub-tool of `wf`. It records the
shape we agreed on and the reasoning behind each decision, so later work has one
place to consult rather than rediscovering the trade-offs.

---

## 1. Motivation and boundaries

Two genuinely different concerns must never be fused: *what a project is* (where
it lives, which branch, resolved build/install dirs, git state) and *how to build
it* (toolchain/preset layering, command generation). Fuse them and every
read-only operation — listing, validating, reporting — gets dragged through build
*planning* just to introspect, which is complexity no one asked for.

A subtler trap lies in the layering itself. If each configuration layer's
templates can reference *and overwrite* the previous layer's resolved values, the
resolver has to keep looping back on itself — rebuilding its context and
re-asserting authoritative values after every layer. The design forecloses that
by making the layering strictly one-directional (§5).

The design draws two hard lines.

### 1.1 The read/act split

A small, pure, **read-only core** describes and resolves; heavier **action**
modules build, update, and manage per-branch build contexts on top of it. The
core has no side effects beyond reading config and git state, so scripts and
other tools (notably `wf stack`, §9) can consume it freely; the actions'
complexity never leaks back into it.

### 1.2 We are a mechanism, not a policy engine

The single most clarifying principle, relied on everywhere below:

> **We apply what the user declared; we do not transform it, and we do not
> guess.** Overrides passed on the command line or via the environment are
> layered on verbatim — the tool never reinterprets what a `-DFOO=BAR` *means*.
> The one and only place we translate is a *declared toolchain's* canonical
> fields into a build system's native spelling (§5.4), because that is the whole
> point of declaring a compiler once. Everything else is pass-through.

This principle is why there are no built-in toolchains, no runtime linker
probing, no path-inferred organisations, and no clever "fix the drifted remote
URL for you" behaviour. When the tool does less, it surprises less.

### 1.3 CLI surface

The read/act split (§1.1) is carried onto the command line itself: `project` is
the **read** command, while the mutating actions `build` and `update` are their
own top-level commands. `context` stays nested under `project` — unlike
`build`/`update` it has no independent identity of its own (it is always about
*a* project's build context), and it is rare enough not to earn a top-level verb.

```
wf project [<name|path>] [--check]                # describe / list / validate (read-only; the default)
wf project context {create|prune} [<name|path>]   # manage a branch's build context
wf build   [<name|path>]                          # configure + build + (un)install
wf update  [<name|path>]                          # refresh git for a project's repos
```

Each earns the busybox applet forms automatically (`wf-build`, `build`,
`wf-project`, …), like every other `wf` sub-tool. Global `-v/--verbose` and
`-n/--dry-run` are inherited from the `wf` process layer: every mutating action
respects dry-run, every read still runs.

Splitting the commands this way is not a departure from the "one core, many
consumers" shape — `build`, `update`, and `project` all sit on the *same*
read-only core (§1.4); only the CLI grouping changed. Any future project-related
verb makes the same choice independently: nest under `project` if it is rare or
tightly coupled to the read surface, promote to a top-level command if it is
frequent enough to deserve a terse form (as `build`/`update` were). Because the
core neither knows nor cares which side of that line a consumer falls on
(§1.4), the choice is cheap to revisit later.

### 1.4 Library shape — core plus actions

The CLI grouping is *not* the code grouping, and it is *also* not the same as
the crate-module grouping. `project`'s read-only **core** (`model`, `workspace`,
`resolve`, `git`) is a self-contained subsystem, so it lives under `util` as
`util::project`, not inside a command; the build systems live beside it as
`util::build_system`. The `cmd` layer is then thin CLI shells: `cmd::project`
(describe / `--check`, plus the CLI-nested `context` action), `cmd::build`, and
`cmd::update` all consume the core's public API — `resolve_target`,
`resolve::plan`, `git` — the same way an external tool would:

```
                        util::project
          (read-only core: model / workspace / resolve / git)
            ▲             ▲             ▲                ▲
            │             │             │                │ implements
      cmd::project   cmd::build    cmd::update    util::build_system
      (info/check,       │                        (cmake/meson/cargo)
       context)          └──────────── also uses ─────────┘
```

The build systems (`cmake`/`meson`/`cargo`) live in `util::build_system`,
beside the core rather than inside any command, because emitting build steps is
a purely build-time concern the core never touches. The core has exactly one
tie to them: translating a *selected toolchain* into a backend's native
env/definitions at L0 (§5.4). That tie is a one-method seam,
`resolve::ToolchainInjector`, **defined by the core (`util::project`) but
implemented by each backend (`util::build_system`)**; `cmd::build` resolves the
chosen backend and hands it to `resolve::plan` as the injector, while path-only
callers (`context`, `info`) inject nothing. So `util::project` exposes no
`Backend`, `Step`, `EmitContext`, or build-system registry at all — only the
abstract seam — and the dependency still points one way: `build_system` →
`project`, and each `cmd` shell → the core, never back into a command.

`context` did not move out alongside `build`/`update` because it is CLI-nested
under `project` (§1.3) as well as code-nested — the two questions happened to
agree here, but they are still independent (§1.3). If `context` (or a future
action) is ever promoted to a top-level command, it should move to its own
`cmd` module at the same time, for the same reason `build`/`update` did: an
action with its own top-level verb has no business reaching into `project`'s
private internals, only its public API.

### 1.5 Out of scope

Git hooks, the `bin/git` safety proxy, and `ssh-key.sh` stay as shell. `project`
is a *programmatic* tool only. **Cross-project dependencies are out of scope**:
building one project never triggers building another. Dependency compilation is
rare, and supporting it would drag in a whole graph / topological-order /
profile-propagation subsystem for little benefit. A `build` builds exactly one
project.

---

## 2. Core concepts

| Concept | One-line definition |
|---|---|
| **Repo** | One git checkout — a `path`, remotes, a main branch, lifecycle hooks, a branch strategy, and an `anchor`. Whether it is standalone / submodule / subtree is *inferred* from its path and `main_branch` (§3.5), never declared. |
| **Project** | A buildable unit: references one or more Repos as `[repos.NAME]` (a `repos.main` is always required), names which one is the `focus`, plus build configuration. |
| **Profile** | The axes that affect *resolution*: `build_type`, `toolchain`, `generator`, `branch`, and the active `presets`. |
| **Preset** | A reusable, named, inheritable bundle of build config across three levels (org → project → repo). |
| **Toolchain** | A named, user-declared set of compilers/tools/flags, *selected* per build and *translated* to a backend's native form. |
| **Backend** | A build system (cmake / meson / cargo …). The tool's only extension axis. |
| **Build context** | A branch's physical build space: a worktree (+ build dir) or just a build dir, depending on strategy (§8.3). |

The unifying principle, stated once and relied on everywhere:

> **A submodule is just a Repo whose working path happens to be nested.** It is a
> normal `[repos.NAME]` entry, managed exactly like a top-level Repo (same
> remotes, branch, hooks, update logic). The only difference is *where it sits*,
> never *how git is handled*.

---

## 3. Repo model

A Repo describes one git checkout. A Project may have several (§4); the fields
below apply to each `[repos.NAME]`.

### 3.1 Remotes and roles — additive only

Remotes are declared with **roles**, so commands never guess which remote does
what:

- **`origin`** — the repo we work on (often our fork). Default fetch + push.
- **`upstream`** — the PR / sync target. When absent, it logically falls back to
  `origin` (we do not create a git remote for it in that case).
- **`mirrors`** — extra *push* URLs attached to `origin`, so one push fans out.

```toml
[repos.main.remotes]
origin   = "git@github.com:me/mesa.git"
upstream = "https://gitlab.freedesktop.org/mesa/mesa.git"
mirrors  = ["git@codeberg.org:me/mesa.git"]
```

The reconciliation `update` performs is deliberately **additive only, never
modifying**: a declared remote that is missing is added; a declared remote that
already exists is **left exactly as-is** — its fetch URL is never "corrected",
and no warning is emitted, because the URL is the user's to own (§1.2). Missing
mirror push-URLs are added; existing push-URLs are never removed. Remotes the
config does not mention are never touched. The one non-obvious mechanic worth
recording: git stops defaulting `push` to the fetch URL once *any* push URL is
added, so a mirror setup's push-URL list must include the origin URL itself
(`{origin} ∪ mirrors`) — `update` only ever *adds* toward that set.

### 3.2 Main branch

A Repo with its **own git** (standalone / submodule) **must** declare its main
branch explicitly; we do not auto-guess. A `subtree` has no own git, so it has no
main branch — it follows its `anchor`.

### 3.3 Lifecycle and hooks

`clone` and `update` are phased lifecycles. Each phase has a **default action
that can be replaced wholesale**, plus pre/post hooks. Hooks are **inline
templated command strings**, run with `sh -c`:

```
clone:   pre → action (default: git clone + set up remotes + checkout + submodule init) → post
update:  ensure-remotes → pre → action (default: §7) → post
```

The hook contract (settled, and the reason it is simple):

- **cwd is always the repo's `path`**, in whatever working-tree state `update`
  left it. The default `update` action does *not* switch branches (§7), so a hook
  sees the current checkout, not necessarily `main`. A hook that needs to act on
  the main branch references its ref explicitly (`{{repo.main_branch}}`) rather
  than assuming a checkout.
- **Overriding the `action` hands the user full control.** We run their string
  verbatim in the repo cwd — including any branch switching they choose to do —
  and do not switch back for them. This is §1.2 applied to lifecycles: the smart
  no-switch behaviour is the *default action's*, not a wrapper we impose on
  overrides.
- **Any hook exiting non-zero fails fast** (§7): the operation stops, the RAII
  restore guard returns the repo to its original branch, a log line is written,
  and the program exits non-zero. State is never left half-switched.

### 3.4 Branch strategy — worktree vs in-place

Each Repo chooses how multi-branch work is physically realized:

```toml
[repos.main]
branch_strategy = "in-place"    # or "worktree"; default "in-place"
worktree_dir    = "{{repo.path}}.worktrees/{{branch.slug}}"   # used when worktree
```

Both are first-class. They are unified behind one resolved variable
**`{{work.dir}}`** = "the directory where source is checked out for *this*
build" (§6):

- **in-place**: `{{work.dir}}` = the clone path. Building a non-current branch
  runs the classic dance — stash → switch → build → switch back → pop — driven by
  an **RAII restore guard** that returns to the original branch and pops the stash
  on *any* exit (success, error, or panic). A dirty tree is always auto-stashed;
  because the guard always restores, this is safe and needs no config knob.
- **worktree**: `{{work.dir}}` = the resolved `worktree_dir` for the target
  branch. `build` **requires the worktree to already exist** and errors if it
  does not — it never implicitly creates one. Worktrees are created explicitly
  via `project context create` (§8.3), which also handles the sparse-checkout and
  submodule details.

Because everything downstream references `{{work.dir}}`, switching strategy is
transparent to the rest of the config. In a *build*, `{{work.dir}}` is the **build
repo** (the focus's `anchor`, §4.1); when the focus builds itself the two are the
same repo, which is the case this strategy is defined against. How strategy
composes when the focus is nested under a different anchor is a TODO (§12).

### 3.5 Topology — `path`, `anchor`, and inferred kind

Every project has a **required `[repos.main]`** — the reserved root and entry
point. Other repos hang off it.

```toml
[repos.main]                       # REQUIRED reserved root; standalone
path        = "~/src/mesa"
main_branch = "main"

[repos.inner]                      # inferred submodule: nested path + own main_branch
path        = "subprojects/inner"  # subpath, relative to repos.main
main_branch = "develop"
anchor      = "main"               # build via the root (else builds itself)
```

- **kind is inferred, never declared** — a repo whose `path` is non-nested (an
  absolute location) is `standalone`; a repo whose `path` is nested (relative to
  `repos.main`) is a `submodule` when it declares its own `main_branch` and a
  `subtree` when it does not. `standalone`/`submodule` have their own git; a
  `subtree` shares its anchor's. `repos.main` is always standalone. `info --check`
  validates the inference against actual git state.
- **`path`** — for `repos.main` and standalone siblings, the on-disk location /
  clone destination; for nested repos, a subpath **relative to `repos.main`**.
- **`anchor`** — names the Repo whose `path` is this Repo's build/config **base**
  (§4.2). It may point at *any* repo, not only `main`. Unset / self → build at
  this repo's own `path`.

There is deliberately **no** separate "source in a subdirectory, build at the
root" axis. `anchor` names one base directory that is both the configure source
and the build base. If a real project ever needs to configure a subdirectory of
the anchor while building at the anchor, a future optional `configure_subdir` can
be added — it is out of v1 because nothing needs it yet.

---

## 4. Projects, repos, and the `focus`

### 4.1 One project, one or more repos

A Project lists its git checkouts as named `[repos.NAME]` tables (with a required
`repos.main`, §3.5) and names which one is the build focus:

```toml
[project]
focus = "main"     # focus repo; defaults to "main"
```

- `focus` selects the **focus** repo — the one you are working on. **Branch
  identity, and the repo switched to your target branch, come from the focus**
  (its own git, or the git it shares when it is a subtree); the focus's own
  submodules are aligned to the target's gitlink on a switch. The **`work.dir`
  (build source) comes from the focus's `anchor`** (§4.2). In the common case the
  focus builds itself (`anchor` = self) and the two coincide. `focus` defaults to
  `main` and can be overridden per-invocation with `--focus <repo>` — invaluable
  in a large monorepo where you switch which component you work on without editing
  config.
- `update` touches **all** of the project's repos; `info` reports all of them;
  `build` builds the `focus` (switching *it* to the target branch), sourcing from
  its `anchor`.
- The repos are **local to this project file** — this is *not* repo sharing
  across projects (one project : N repos, never N projects : one repo).

### 4.2 Build source dir: the `anchor`

"Where the build sources from" is decided by each repo's **`anchor`**:

- `anchor` unset / self → the build sources from the repo's **own** `path` (an
  independent build of a submodule, a subtree subdir, or a standalone repo).
- `anchor = "main"` (or any other repo) → the build sources from **that repo's**
  path ("cannot build detached from the root").

This one rule covers every case uniformly — there is no separate "component kind"
or "build at root / source at root" machinery. A subtree that must build via the
root and a submodule that must build via the root are spelled identically:
`anchor = "main"`.

A consequence worth stating: when a subtree sets `anchor = "main"`, its own
subpath carries **no git meaning and no build-source meaning** — the configure
source is the anchor's path. The subtree entry then contributes only its *name*
(as the possible `focus`) and its *repo-level presets*. That is intended.

---

## 5. Build configuration layering

Configuration is assembled in one strictly single-directional pass, then handed
to a backend emit step. Nothing is *accumulated and then re-asserted* — the fixed
order guarantees no layer ever needs revisiting.

### 5.1 The pipeline

```
Selection (always) — produces names + paths, no build side effects
  1. resolve config-root → load Workspace → locate the Project (by name/org, or by path)
  2. resolve the Profile axes: build_type / toolchain(name) / generator / branch / presets
  3. resolve path templates that need only names: work.dir, build_dir, install_dir

Accumulation (single-directional; each layer resolved as it merges)
  L0  toolchain injection        → environment / definitions      [skipped when trusting config, §5.3]
        the injector (a backend, via the ToolchainInjector seam) translates the
        toolchain's canonical fields (§5.4); the toolchain's own environment/
        definitions are passed through verbatim. A path-only resolve injects nothing.
  L1  project config             → merge [environment]/[definitions]/extra_*
  L2  presets                    → default_presets, then applies_when matches, then --preset (§5.5)
  L3  CLI extra args             → --extra-*-args / -Xscope,arg  (verbatim, highest priority)
  = final { environment, definitions, extra_config_args, extra_build_args, extra_install_args }

Emit (backend)
  backend.steps(mode)            → ordered [Step{ argv, cwd, env }]; definition→argv spelling is private
```

Because the order is single-directional and no later layer can rewrite a
toolchain's compiler identity (§5.2), **no layer is ever revisited** — there is no
context rebuild between layers and no re-assertion of the toolchain after presets.

Template resolution is not a literal single pass at the very end; each layer's
newly-added keys are resolved *as the layer merges*, against the immutable
context accumulated so far. Intra-layer self-reference (one `environment` entry
referencing another) is handled by the resolver's lazy recursion, not a separate
topological-sort pass (§6.1).

### 5.2 The toolchain hard constraint

**A toolchain's compiler identity (cc/cxx/rustc/linker/launcher) enters the
context as an immutable input; presets cannot overwrite it.** Changing the
compiler is done only by moving along the selection chain (§5.5), never by
patching from a preset. This is the constraint that makes the pipeline
single-directional: since a preset can never clobber the toolchain, there is
never a need to re-assert the toolchain after presets. (A user who *explicitly*
sets, say, `CMAKE_C_COMPILER` via `-Xconfig` is exercising §1.2 pass-through, not
re-asserting a toolchain — and their explicit value wins, as the highest layer.)

### 5.3 Selection vs injection, and trusting an existing config

Two things that are easy to conflate but must stay separate:

- **Selection** — the toolchain's *name* is always resolved, because path
  templates (`build_dir = ".../{{toolchain}}"`) depend on it.
- **Injection** — merging the toolchain's env/definitions into the build.

Injection (L0) is **skipped** in `auto`/`build-only` mode when the build
directory is already configured and no toolchain was explicitly requested — so
re-running `build` does not trigger a needless reconfigure. Selection always
happens; injection is conditional. In `config-only`/`reconfig` the toolchain is
always injected, and it can be swapped for that run with `--toolchain` or the
environment (§5.5) *without editing config*.

### 5.4 Single source of truth for compilers, realised by the backend

A compiler is declared **once**, in a toolchain, using a backend-agnostic
vocabulary aligned with meson's native file:

```
cc  cxx  rustc  ar  nm  ranlib  strip  linker  launcher      # binaries
c_flags  cxx_flags  link_flags                               # flags
```

The **backend translates** these canonical fields into its native form — this is
the *only* translation the tool performs (§1.2). Each canonical field maps to a
**universal environment variable** (`CC`, `CXX`, `AR`, …, `CFLAGS`, `RUSTC`,
which nearly every tool honours) **plus a backend-native definition** where one
exists (`CMAKE_C_COMPILER`, `CMAKE_AR`, …; meson is nearly 1:1 and can emit a
native file directly; cargo maps `rustc → RUSTC`, `launcher → RUSTC_WRAPPER`).
One `cc = "clang"` declaration is therefore correct under every backend, written
once. Because this mapping runs at **L0**, an explicit preset or CLI override of
the same key still wins.

Anything outside the canonical vocabulary (e.g. an exotic tool) goes in the
toolchain's `environment` block and is passed through untranslated.

### 5.5 Toolchains are *selected*, not rewritten — and there are no built-ins

Toolchains are **100% user-declared**; the tool ships **no built-in toolchain
definitions**. Overriding a toolchain means moving along a **selection chain**,
never patching a toolchain's internals per-project:

```
user config [toolchains]  →  project/repo's toolchain field  →  CLI --toolchain / env
```

Environment beats `--toolchain` (consistent with the codebase's "env is the
deliberate, ephemeral override" philosophy, §10). Per-project rewriting of a
toolchain's internals is not supported by design.

### 5.6 Presets

- **Three levels with cross-level merge**: presets exist at **org → project →
  repo**, declared via `[org.presets.NAME]`, `[project.presets.NAME]`,
  `[repos.<focus>.presets.NAME]`. A referenced name is the *merge* of the
  same-named preset at each level. **Maps** (environment/definitions) merge by
  key with the **nearest (most specific) level winning**; **lists** (extra args)
  are **replaced by the nearest level** — the most specific level's list is the
  one that applies, exactly as a command line's nearest occurrence wins. There is
  no system/global level (toolchains are the system concern, §5.5).
- **Cross-org references**: a reference may be org-qualified as `<org>/<preset>`
  to pull from another org (invaluable for LLVM-family projects sharing an `llvm`
  org). Works in `extends`, `default_presets`, and `--preset`.
- **Inheritance**: `extends` is kept. The merged same-named definition is formed
  first; its `extends` are then resolved from that merged form.
- **Auto-application by structured match**: rather than arbitrary conditions, a
  preset declares `applies_when` over a fixed key set — `build_type`,
  `toolchain`, `os`, `arch`, `generator`. Keys are AND-ed; a key's value is a
  scalar (equality) or an array (membership/OR); comparison is **case-sensitive**.
  A match auto-applies the preset. A project may also list `default_presets` that
  always apply.

```toml
[project.presets.dev]
extends      = ["llvm/base", "debug"]
applies_when = { build_type = "debug", toolchain = ["clang", "clang-cl"] }
definitions  = { ASSERTS = true }

[project]
default_presets = ["warnings"]
```

The application order is `default_presets` → `applies_when` matches → `--preset`,
de-duplicated by name keeping the **last** position (so an explicitly-passed
preset moves late and thereby wins). CLI `-X`/`--extra-*-args` (L3) sit above all
of it. Auto-application is deliberately a *structured* match rather than an
arbitrary expression: predictable, and easy to validate ahead of time.

---

## 6. Templates, Profile, and path resolution

### 6.1 Template engine (minimal, `core::template`)

Config is **TOML only**. The engine is a zero-domain `{{ }}` / `[[ ]]` evaluator,
reusable and unit-testable in isolation:

- **`{{ path.to.var }}`** — dotted lookup over the context (tables by key, arrays
  by integer index). A whole-string single placeholder returns the **typed** value
  (a list or int survives); an embedded placeholder is stringified. Resolution is
  lazy and recursive with memoisation and cycle detection, so one `environment`
  entry referencing another simply resolves on demand — no separate dependency-map
  or topological-sort pass is needed.
- **`[[ … ]]`** — a *minimal* numeric expression, kept for real needs like
  `LINK_JOBS = "[[ max(1, system.memory.total_gb // 4) ]]"`. Scope: `+ - * / // %`
  over int/float, comparisons, and the functions `min max int float str bool`.
  It is **not** a general expression language — no `**`/bitops, no `and`/`or`, no
  ternary, no arbitrary names, no list/dict literals. Arbitrary conditions are
  handled by `applies_when` (§5.6), not here.

Error semantics: every failure is a hard error — unknown path, cycle, type
mismatch, division by zero. The context is **always fully populated** (optional
values like `upstream` are filled with their fallback at assembly time), so a
missing path always means a real mistake, never a silent empty string.

### 6.2 Context variables

```
project.{ name, org, focus }
repo.*                     # the *current* repo (the focus repo in project scope;
                           #   the repo itself in a repo-scoped field such as a hook)
  { name, path, kind, main_branch, anchor, origin, upstream, mirrors }
repos.<name>.*             # any repo by explicit name; same fields as repo.*
work.dir                   # the effective checkout dir for this build (§3.4)
branch.{ raw, slug }       # raw = the branch name; slug = filesystem-sanitised
build_type
toolchain.{ name, cc, cxx, rustc, ar, nm, ranlib, strip, linker, launcher,
            c_flags, cxx_flags, link_flags }
generator
system.{ os, arch, memory.total_gb, cpu.count }
env.*                      # process environment
```

`repo` is a **relative** alias for *the repo currently being resolved*, never a
synonym for a fixed repo; cross-references always use `repos.<name>`. There is no
bare `{{branch}}` — a template must pick `.raw` or `.slug`, so there is never
ambiguity between the raw name and its sanitised form.

### 6.3 Profile vs BuildOptions

The axes that affect *resolution* are separated from the options that affect only
*command steps* — a distinction worth keeping crisp:

```rust
pub struct Profile {          // affects identity / build_dir / work.dir resolution
    build_type: String,
    toolchain:  Option<String>,   // None → selection chain (§5.5)
    generator:  Option<String>,
    branch:     Option<String>,   // None → the focus repo's current branch
    presets:    Vec<String>,
}

pub struct BuildOptions {     // affects command steps only
    mode:    BuildMode,           // auto | config-only | build-only | reconfig | uninstall
    install: bool,
    target:  Option<String>,
    extra_config_args:  Vec<String>,
    extra_build_args:   Vec<String>,
    extra_install_args: Vec<String>,
}
```

`info --branch X`, a hook resolving a dir for a deleted branch, and `build` all
share the same `Profile` to resolve paths; `BuildOptions` appears only when a
build actually executes.

### 6.4 Branch identity

Identity **is the branch name, and only that.** The richer five-layer waterfall
(ref-tip → Change-Id → slug → hash) sometimes proposed for stacked diffs is
deliberately **out of scope**: even under stacked-diffs, driving from one stable
branch
gives the whole experience and stays compatible with existing scripts, at a
fraction of the complexity. A **detached HEAD is not supported** — `build`
requires a branch (its own, or `--branch`). `branch.slug` sanitises the name by
replacing every character outside `[A-Za-z0-9._-]` (including `/`) with `_`.

---

## 7. `update` / `clone` semantics

Fusing "update the repo" with "prepare to build a specific branch" is what
produces a tangled root/component switch-stash-restore dance, so the two are kept
separate: **`update` never switches branches or touches your working tree**
(unless you happen to be standing on the main branch), and it
treats every repo uniformly because a submodule is just a nested Repo (§2).

```
update(project):
  for repo in repos, parents before nested (subtree contributes no git work):
    if repo.path is missing → clone lifecycle
    else                    → ensure-remotes (additive, §3.1) → pre → action → post

default update action:
  if currently on main_branch:  git fetch --all ; git merge --ff-only <upstream>/<main_branch>
  else:                         git fetch <origin> <main_branch>:<main_branch>   # ref-only fast-forward
  then advance declared submodule repos (their own lifecycle), and refresh
       undeclared nested submodules with: git submodule update --recursive -- <materialised paths>
```

The pivotal simplification is the **no-switch default**: when you are on a feature
branch and run `update`, we fast-forward the `main` *ref* with a refspec and never
check it out — so there is nothing to stash, switch, or restore. The dance only
appears in the narrow case where you are already standing on the main branch.

**Sparse-checkout is safe by design.** A refspec fetch updates refs and objects
without ever expanding the sparse cone; an `--ff-only` merge honours the cone; the
submodule refresh is limited to explicitly-passed, already-materialised paths and
never uses `--init`. `--init` appears only on a *fresh working-tree event* —
clone, or worktree creation (§8.3) — never on update.

**Failure is fail-fast with guaranteed restoration.** A hook or action exiting
non-zero stops the operation immediately, the RAII guard returns the repo to its
original branch (and pops any stash), a log line is written, remaining repos are
skipped, and the program exits non-zero.

---

## 8. `build`, backends, and build contexts

### 8.1 The backend abstraction — the only extension axis

A new build system is a new `Backend` impl plus registration. Backends live
in `util::build_system` (§1.4), never in the core; the core and the resolver
name no concrete backend. The abstraction is split so the core depends only on
the half it needs — the L0 toolchain translation — via the core-owned
`ToolchainInjector` seam:

```rust
// in util::project::resolve (core): the only backend-facing thing the pipeline sees
trait ToolchainInjector {
    fn apply_toolchain(&self, tc: &Toolchain, cfg: &mut LogicalConfig);   // L0
}

// in util::build_system: the full build-time abstraction, a ToolchainInjector plus emission
trait Backend: ToolchainInjector {
    fn name(&self) -> &str;                        // "cmake" | "meson" | "cargo"
    fn steps(&self, ctx: &EmitContext) -> anyhow::Result<Vec<Step>>;
    fn is_configured(&self, build_dir: &Path) -> bool;
}
```

`apply_toolchain` runs at **L0** (so overrides can win, §5.4) and is the *only*
backend method the core invokes — through the seam, given the concrete backend
by `build`. `steps` runs at emit and owns the definition→argv spelling (cmake's
`-DK:TYPE=V` vs meson's `-Dk=v`), the command sequence per `BuildMode`, and
`is_configured` detection (cmake's `CMakeCache.txt`, meson's `coredata.dat`,
none for cargo) — none of which the core ever sees.

Whether a declared `build_system` actually *has* a backend is reported by
`wf build` at run time, not by `wf project --check`: the core does not know the
set of supported build systems, so `--check` validates only declared-fact
consistency (e.g. a toolchain's `supports` list vs the `build_system`).

### 8.2 Modes, install, and uninstall

`BuildMode` is `auto | config-only | build-only | reconfig | uninstall`.
`--install` adds an install step to a build. `uninstall` is its own mode because
install is **not** a plain `rm`: an install dir can equal the build dir or be a
shared prefix like `$HOME/.local` mixed with other projects. Uninstall is
therefore backend-driven — meson `ninja -C <build> uninstall`, cmake via
`install_manifest.txt`, cargo unsupported — never a recursive delete.

### 8.3 Build contexts — one set of semantics for both strategies

A branch's **build context** is its physical build space, which differs by
strategy: a worktree plus a build dir (worktree strategy), or just a build dir
(in-place strategy). `project context` manages it **strategy-transparently**:

- **`context create <name> --branch X`** — worktree strategy: `git worktree add`
  at the resolved `worktree_dir`, idempotent (an existing one is reported, not
  re-created), erroring if X is already checked out elsewhere (git's one-branch-
  one-worktree rule). If the source is sparse, the worktree is created
  `--no-checkout`, the sparse patterns are replicated, then checked out, so it
  materialises only the cone; its submodules are then initialised within the cone.
  In-place strategy: a no-op (there is no worktree; the build dir is created
  lazily by `build`).
- **`context prune <name> --branch X`** — tears down X's build context
  **regardless of strategy**: remove the worktree (if any) and delete `build_dir`.
  It deliberately **never touches `install_dir`** (see §8.2); install reversal is
  `build --uninstall`.

The payoff is for git hooks: on branch deletion a hook calls `context prune X`
and the tool does the right thing for that repo's strategy — the hook never needs
to know whether it was a worktree or in-place. `build` under the worktree
strategy **requires** the context to exist and errors otherwise; it never creates
one implicitly.

---

## 9. Crate API and CLI contract

### 9.1 The crate API (read-only, consumer-driven)

The core is a read-only library other tools query. It **resolves** paths but
never destroys them — the only side-effecting entry points are the `build`,
`update`, and `context` actions.

```rust
let ws = Workspace::load(config_root)?;
ws.projects();                        // iterate all
ws.project("mesa", org)?;             // by name / org
ws.project_for_path(&path);           // reverse lookup: which project owns this checkout?

let p = ws.project(...)?;
p.repos();  p.build_repo();           // all repos / the focus repo
p.main_branch();                      // e.g. wf stack's base-branch resolution
p.git_state();                        // branch, commit, dirty, submodules (read-only)
p.work_dir(&profile);                 // resolved work.dir (worktree or in-place)
p.build_dir(&profile);  p.install_dir(&profile);
p.resolve("{{ ... }}", &profile);     // expose the resolver to callers
p.validate();                         // structural issues (info --check)
```

Two real consumers shaped this surface: **`wf stack`** needs
`project_for_path → main_branch` to resolve its base branch from a checkout's
path (see `docs/stack/design.md` §5.1); **git hooks** need `build_dir`/`work_dir`
resolution for an arbitrary branch to clean up after a deleted branch (§8.3).

### 9.2 CLI contract

- **`project [<name>]`** — the read command. No name lists a summary of every
  project; a name gives details. With `Profile` flags it shows resolved
  build/install/work dirs; without them it shows the raw templates. It also lists
  a project's worktrees and their resolved dirs. Pure read.
- **`project --check [<name>]`** — config-legality validation (required fields,
  valid build system, preset/inheritance cycles, template resolvability, toolchain
  references exist). No name checks everything (CI use). A `--check` mode of the
  read command rather than a separate verb.
- **`build <name>`** — resolves a full `Profile`, runs the §5 pipeline and the
  backend emit, honouring the focus repo's branch strategy.
- **`update [<name>]`** — the §7 lifecycle over all of the project's repos; no
  name updates every project.
- **`project context {create,prune} <name> --branch X`** — §8.3.

Every verb's positional is a **name or a path**, mutually exclusive. A token that
is `.`/`..` or begins with `.`, `/`, or `~` is a **path** — it may point *inside*
a checkout, and the owning project is found by deepest-prefix match
(`project_for_path`, §9.1); anything else is a **name**, bare or fully-qualified
`org/name`. A bare name ambiguous across orgs is a hard error asking for
qualification; there is no `--org` filter. When the positional is omitted, `info`
covers every project while `build`/`update`/`context` operate on the project
owning the current directory (a hard error if none does). (Shells expand `~` and
leave `./` literal, so in practice the classifier keys on a leading `.` or `/`.)

---

## 10. Configuration topology

Configuration is **content-addressed, not path-addressed**: files may live
anywhere under one config-root and declare what they are via their TOML sections.

### 10.1 Config-root resolution

Exactly one config-root, resolved in priority order:

```
$WITS_PROJECT_CONFIG (env)  >  $XDG_CONFIG_HOME/wits/project  >  $HOME/.wits/project
```

A fixed user location (rather than a `$PWD`-relative one) is deliberate: `wf
stack`'s path reverse-lookup and hook-driven cleanup must find *the same* project
registry regardless of the current directory. Environment is the single explicit
override, consistent with §5.5.

### 10.2 One project per file; registries merge

- A file containing `[project]` (with a required `repos.main`) **is one project**.
  The same `(org, name)` appearing in two files is a **hard error**, not a silent
  override — cross-file layering of a single project would be a confusion source.
- `[toolchains.*]` and `[org]` + `[org.presets.*]` are **additive registries**
  that freely merge across the whole tree.
- The root is scanned recursively for `*.toml` **eagerly** at `Workspace::load`;
  every file is loaded and its top-level sections routed. A single file may mix
  sections.

### 10.3 `org` is always explicit

An org is declared with `[org] name = "…"` and joined with `project.org`. It is
**never inferred from the file path**, because placement is arbitrary.

---

## 11. Naming

The tool's configuration and environment namespace is **`wits`**: config lives
under `wits/project`, environment overrides are `WITS_*`. (The umbrella binary
name is a separate, out-of-scope concern.)

---

## 12. Open questions / future

- **[open]** Output *format* of `info` (`--json` rejected as the answer; the exact
  human/scriptable format is to be designed).
- **future** A `configure_subdir` axis, only if a real "source in a subdir, build
  at the anchor" project appears (§3.5).
- **future** Which backends ship in v1 (cmake / meson / cargo are confirmed real;
  bazel / make pending a real need).
- **future** The finer points of submodules inside worktrees beyond the v1 rule
  in §8.3.
- **TODO** Whether a *nested focus* (a `focus` whose `anchor` is not itself)
  should exist at all — it may be simplified away. If it stays, how
  `branch_strategy = "worktree"` composes with it is unresolved: worktree-ing a
  nested component while building from its anchor is not yet coherent. For now the
  focus/anchor roles are defined (§4) but a nested focus is always switched
  **in-place**, and a worktree of one is unspecified.
- **out of scope** The five-layer branch identity (§6.4) and cross-project
  dependencies (§1.5) — recorded here so they are not re-proposed by reflex.
