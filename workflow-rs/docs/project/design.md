# `wf project` — Design

> Status: **design draft**, no code yet. This document is the agreed shape of the
> `project` tool before implementation. Items marked **[open]** still need a
> decision; everything else is settled in discussion and may still change as we
> implement, but should not be reopened casually.

This is the reference design for the rewrite of the old Python `builder` into a
`wf` sub-tool. Future docs should link here rather than restate it.

---

## 1. Motivation and boundaries

The old `builder` fused two genuinely different concerns into one `plan()`
method: *what a project is* (where it lives, which branch, resolved build/install
dirs, dependency chain, git state) and *how to build it* (toolchain/preset
layering, command generation). The proof of the coupling: `list`, `validate`, and
`update` all had to run the full build planner just to introspect — they wanted
to *read*, but were dragged through build *planning*. That coupling is the source
of the accidental complexity we are removing.

The rewrite splits along that seam.

**CLI surface** — one umbrella noun, three verbs:

```
wf project info   [<name>] [profile flags]
wf project build  <name>  [profile flags] [mode flags]
wf project update [<name>] [--all]
```

`project` earns the busybox applet forms automatically (`wf-project`,
`project`), like every other `wf` sub-tool.

**Library shape** — the CLI grouping is *not* the code grouping. The library is a
small pure **core** plus two heavier **actions** built on top of it:

```
project (lib core)   — describe + resolve + git introspection (read-only)
   ▲          ▲          this is the reusable crate API
   │          │
 build      update       — action modules, depend on the core
   │          │
   └────┬─────┘
   wf project {info,build,update}   — thin CLI shell
```

The core has no side effects beyond reading config and git state, so scripts and
other tools can consume it freely. `build` and `update` are consumers; their
complexity must never leak back into the core.

**Out of scope / kept elsewhere:** git hooks, the `bin/git` safety proxy, and
`ssh-key.sh` stay as shell. `project` is a *programmatic* tool only.

---

## 2. Core concepts

| Concept | One-line definition |
|---|---|
| **Repo** | One git checkout — a `path`, a `kind` (standalone / submodule / subtree), remotes, a main branch, lifecycle hooks, a branch strategy, worktrees, and an `anchor`. |
| **Project** | A buildable unit: references **one or more** Repos as `[repos.NAME]` (a `repos.main` is always required), names which one to `build_in`, plus build configuration. |
| **Profile** | The build-time axes: `build_type`, `toolchain`, `generator`, branch-identity, and the active `presets`. |
| **Preset** | A reusable, named, inheritable bundle of build config. The "weapon" for switching configurations fast. |
| **Toolchain** | A named declaration of compilers/linker/launcher and their config, *selected* (not rewritten) per build. |
| **ResolvedProject** | The pure-data product of the core: everything `info` reports and everything `build`/`update` consume. |

Key unifying principle, stated once and relied on everywhere:

> **A submodule is just a Repo whose working path happens to be nested.** It is a
> normal `[repos.NAME]` entry, managed exactly like a top-level Repo (same
> remotes, branch, hooks, update logic). The only difference is *where it sits*,
> never *how git is handled*.

---

## 3. Repo model

A Repo describes one git checkout. A Project may have several (§4); the fields
below apply to each `[repos.NAME]`.

### 3.1 Remotes and roles

Remotes are declared with **roles**, so commands never guess which remote does
what:

- **`origin`** — the repo we work on (often our fork). Default fetch + push.
- **`upstream`** — the PR / sync target. When absent, it logically falls back to
  `origin`.
- **`mirrors`** — extra *push* URLs attached to `origin` (one push fans out to
  all). Normal for personal projects: one origin, several mirrors.

```toml
[repos.main.remotes]
origin   = "git@github.com:me/mesa.git"
upstream = "https://gitlab.freedesktop.org/mesa/mesa.git"
mirrors  = ["git@codeberg.org:me/mesa.git"]
```

`update` **idempotently ensures** these remotes exist and are correct on every
run (adds missing, fixes drifted URLs), then **fetches all of them** — upstream
included, not just origin.

### 3.2 Main branch

A Repo with its **own git** (`main` / standalone / submodule) **must** declare
its main branch explicitly; we do not auto-guess. A `subtree` has no own git, so
it has no main branch — it follows its `anchor`.

```toml
[repos.main]
main_branch = "main"
```

### 3.3 Lifecycle and hooks

`clone` and `update` are modeled as phased lifecycles. Each phase has a **default
action that can be replaced wholesale**, plus pre/post hooks. Hooks are **inline
templated command strings** (not a named registry):

```
clone:   pre → action (default: git clone, overridable) → post
update:  pre → action (default: fetch-all + ff-merge, overridable) → post
```

```toml
[repos.main.hooks]
pre_update  = "echo updating {{repo.name}}"
update      = "git fetch --all && git merge --ff-only {{repo.upstream}}/{{repo.main_branch}}"  # optional override
post_update = "git submodule sync --recursive"
```

Hook execution context (cwd, available `{{ }}` vars) is defined in §5/§6.

### 3.4 Branch strategy — worktree vs in-place (both first-class)

Each Repo chooses how multi-branch work is physically realized:

```toml
[repos.main]
branch_strategy = "in-place"   # or "worktree"; default "in-place"
worktree_dir    = "{{repo.path}}.worktrees/{{branch.slug}}"  # used when worktree
```

Both strategies are **fully supported**; worktree is the newer addition, not a
replacement.

The two strategies are unified behind a single resolved variable
**`{{work.dir}}`** = "the directory where source is checked out for *this*
build":

- **in-place**: `{{work.dir}}` = the clone path (`{{repo.path}}`). Building a
  non-current branch runs the classic dance — *stash dirty changes → switch →
  build → switch back → pop* — and must restore the original state even on
  failure.
- **worktree**: `{{work.dir}}` = the resolved `worktree_dir` for the target
  branch. We `git worktree add` it if missing and build there, **never touching
  the user's current working tree**; parallel multi-branch builds become
  possible.

Because the rest of the config references `{{work.dir}}` (e.g.
`build_dir = "{{work.dir}}/_build/{{build_type}}"`), switching strategy is
transparent to everything downstream.

**Submodule + worktree.** This combination is supported — worktrees of a repo
that has submodules are possible. The exact handling of how an outer worktree and
its submodules co-exist (shared object store vs per-worktree submodule checkouts)
is a detail to settle during implementation, not a blocker.

### 3.5 Topology — `kind`, `path`, `anchor`

Every project has a **required `[repos.main]`** — the root clone and entry point.
Other repos hang off it.

```toml
[repos.main]                       # REQUIRED root; standalone
path        = "~/src/mesa"         # clone destination
main_branch = "main"

[repos.inner]                      # a nested submodule
kind        = "submodule"
path        = "subprojects/inner"  # subpath, relative to repos.main
main_branch = "develop"
anchor      = "main"               # optional: build via the root (else builds itself)
```

- **`kind`** — `standalone` (default) | `submodule` | `subtree`. Explicit,
  because it cannot be derived from the path. `standalone`/`submodule` have their
  own git; `subtree` does not (its git is its anchor's).
- **`path`** — for `repos.main` and standalone siblings, the on-disk location /
  clone destination; for nested `submodule`/`subtree`, a subpath **relative to
  `repos.main`**. (This locating role is what replaced the old `parent`.)
- **`anchor`** — the single field that merged `parent` + `build_anchor`. It names
  the Repo whose `path` is this Repo's build **source dir**:
  - unset / self → build at its own `path` (independent build).
  - another repo (e.g. `main`) → build via that repo's path ("cannot build
    detached from the root").
- **`main_branch`** — required for own-git repos (§3.2); on `update` a submodule
  is advanced to the latest of its branch, not the gitlink-recorded SHA.

The two monorepo shapes:

- **subtree monorepo** — `repos.main` (the root) plus a `[repos.<sub>]` with
  `kind = "subtree"` and a subpath. The subtree shares the root's git; it has no
  `main_branch`.
- **submodule monorepo** — a **two-repo** project: `repos.main` (the root, **not
  omittable** — the submodule is cloned *through* it) plus a `[repos.<sub>]` with
  `kind = "submodule"` and its own git/`main_branch`. There is **no project-level
  main branch**; each own-git repo declares its own (§3.2).

In both, building "with the root" is just `anchor = "main"` on the worked-in
repo; building it alone is `anchor` unset/self.

---

## 4. Projects, repos, and the `anchor`

### 4.1 One project, one or more repos

A Project lists its git checkouts as named `[repos.NAME]` tables (with a required
`repos.main`, §3.5) and names which one is the build focus:

```toml
[project]
build_in = "main"     # focus repo; defaults to "main"
```

- `build_in` selects the **focus** repo: branch-identity / `build_dir` / worktree
  derive from it. Defaults to `main`.
- `update` touches **all** of the project's repos; `info` reports all of them;
  `build` builds the `build_in` repo (whose `anchor` decides the source dir).
- The repos are **local to this project file** — this is *not* repo sharing
  across projects (one project : N repos, never N projects : one repo).
- Distinct from `[[dependencies]]`: `[repos.*]` are the checkouts that
  *constitute this one project*; dependencies are *other projects* built first in
  topological order.

### 4.2 Build source dir: the `anchor`

"Where the build's source dir is" is decided by each repo's **`anchor`** (§3.5) —
the single field that replaced the old `component` / `build_anchor` / `parent`
tangle:

- `anchor` unset / self → the build sources from the repo's **own** `path` (an
  independent build of a submodule, a subtree subdir, or a standalone repo).
- `anchor = "main"` (or any other repo) → the build sources from **that repo's**
  path ("cannot build detached from the root").

This one rule covers every earlier case uniformly: there is no `component_kind`,
no `build_at_root` + `source_at_root` boolean pair, and no separate `build_anchor`
enum. A subtree that must build via the root and a submodule that must build via
the root are spelled identically — `anchor = "main"`. `update` always refreshes
**all** repos, so the root comes along regardless of which repo is built.

---

## 5. Build configuration layering

The old `plan()` failed by *accumulating while repeatedly re-asserting*. The new
model is two clean phases.

### 5.1 Phase one — accumulate the logical config

Layer in a single, one-directional precedence order, then **resolve templates
exactly once** at the end:

```
toolchain base          # compilers etc. — the single source of truth
  → project config       # project/repo-carried env / definitions / args
  → presets              # in order, with inheritance; build_type defaults here
  → CLI overrides        # --build-type / -D / -X
= final { environment, definitions, extra_args }
```

### 5.2 Phase two — build-system backend emits commands

Each backend (cmake / meson / cargo / …) takes the *final* logical config plus
the Profile and **derives tool-specific flags at emit time** — e.g. `CC` →
`CMAKE_C_COMPILER`, color diagnostics, `CARGO_TARGET_DIR`. Because derivation
happens after presets, the "re-assert toolchain 3–4 times" hack disappears.

### 5.3 `environment` vs `definitions`

Both are kept; they are different things:

- **`environment`** — the env vars the configure/build runs under.
- **`definitions`** — the build system's `-D` parameters.

Keeping them distinct keeps the overall system environment clean (we don't smear
build options into env vars or vice-versa).

### 5.4 Single source of truth for compilers

A compiler is declared **once**, in the toolchain (`cc`/`cxx`/`rustc`/…).
Build-system spellings like `CMAKE_C_COMPILER` are **derived**, never hand-written
in three places (the old duplication).

### 5.5 Toolchains are *selected*, not rewritten

Toolchains are the system's **default** build definitions — one shared set.
Overriding a toolchain means moving along a **selection chain**, never patching a
toolchain's internals per-project:

```
builtin defaults  →  user config file [toolchains]  →  repo's chosen toolchain  →  CLI --toolchain
```

Per-project local rewriting of a toolchain (e.g. "for this project, clang's CC is
clang-19") is **not** supported by design.

### 5.6 Presets

- **Levels & cross-level inheritance**: presets exist at three levels —
  **org → project → repo** — declared via section namespaces
  (`[org.presets.NAME]`, `[project.presets.NAME]`, `[repos.<build_in>.presets.NAME]`).
  ("repo-level" = the `build_in` repo.) A referenced preset name **inherits down
  that chain**: the result is the *merge* of the same-named preset at each level —
  keys accumulate, and on a key conflict the **nearest (most specific) level
  wins** (repo overrides project overrides org). This is layered on top of a
  preset's own explicit `extends`. There is no system/global preset level
  (toolchains are the system-level concern, §5.5), and no `_select_key` spaghetti.
- **Cross-org references**: a preset reference may be **org-qualified** as
  `<org>/<preset>` to pull from *another* org — invaluable for families like
  LLVM-based projects sharing an `llvm` org. Unqualified names resolve through the
  project's own `org → project → repo` chain; qualified names reach across. Works
  in `extends`, `default_presets`, and CLI `--preset`.
- **Inheritance**: `extends` other presets is kept — the core power.
- **Auto-application instead of arbitrary conditions**: you should *not* have to
  pass `--preset` on every build. Rather than arbitrary `[[ expr ]]` conditions,
  a preset declares a **structured match** over a small fixed key set
  (`build_type` / `toolchain` / `os`, equality only); a match auto-applies it. A
  project may also declare a list of **default presets** always applied.

```toml
[project.presets.dev]
extends      = ["llvm/base", "debug"]        # cross-org + local references
applies_when = { build_type = "debug" }      # structured, not an expression
definitions  = { ASSERTS = true }

[project]
default_presets = ["warnings"]
```

---

## 6. Templates, Profile, and path resolution

### 6.1 Template engine (minimal)

Config format is **TOML only**.

The template engine supports:

- **`{{ path.to.var }}` substitution** over the context (§6.2).
- **A minimal arithmetic subset** inside `[[ ... ]]` — kept specifically for real
  needs like `LINK_JOBS = "[[ system.memory.total_gb // 2 ]]"`. Scope: integer
  arithmetic and comparisons only; **not** a general expression language. Arbitrary
  preset conditions are handled by §5.6's structured match, *not* here.

### 6.2 Context variables (illustrative, to be finalized)

- `repo.*` — the **`build_in`** repo's `path`, `name`, `main_branch`, `origin`,
  `upstream`, …
- `repos.<name>.*` — any specific repo of the project.
- `work.dir` — the effective checkout dir for this build (§3.4).
- `branch`, `branch.slug` — target branch identity (sanitized).
- `project.*` — `name`, `org`, `build_in`, …
- `build_type`, `toolchain`, `generator` — from the Profile.
- `system.*` — `os`, `architecture`, `memory.total_gb`, `cpu.count`.
- `env.*` — process environment.

### 6.3 Profile and the build_dir seam

`build_dir` / `install_dir` templates legitimately depend on build-time axes
(`{{build_type}}`, `{{toolchain}}`). The core therefore holds the *templates* but
resolves a concrete path only when given a **Profile**:

```rust
pub struct Profile {
    build_type: String,
    toolchain:  Option<String>,
    generator:  Option<String>,
    branch:     BranchIdentity,
    presets:    Vec<String>,
}

impl Project {
    fn repos(&self) -> &[Repo];                    // intrinsic, no Profile
    fn build_repo(&self) -> &Repo;                 // the `build_in` repo
    fn dependency_chain(&self) -> Vec<&Project>;   // intrinsic
    fn git_state(&self) -> GitState;               // read-only introspection
    fn build_dir(&self, profile: &Profile) -> PathBuf;   // needs a Profile
}
```

`project info foo` reports intrinsic facts always; build-dependent facts
(build_dir/install_dir) appear only when profile flags are supplied, otherwise the
raw template is shown. `build` always supplies a full Profile. The core never
learns *how to build* — only *how to resolve given a Profile*.

### 6.4 Branch identity

For v1, identity = the (sanitized) branch name; each worktree corresponds to one
identity. The richer **5-layer identity** (branch → ref-tip → Change-Id → slug →
hash) for stacked-diffs is **[open] / future** (§10).

---

## 7. `update` semantics (consolidated)

```
for each Repo of the project (build_in repo, sibling repos, and any submodules):
    1. ensure remotes exist & correct   (origin / upstream / mirrors)   [idempotent]
    2. run pre_update hooks
    3. action: fetch --all  (origin + upstream + mirrors)               [overridable]
              then ff-merge main_branch
    4. submodule repo: advance to latest of its branch (not gitlink SHA)
    5. run post_update hooks
```

A `subtree` repo contributes no extra git work (it shares its anchor's git).
Clone (when a repo's path is missing) uses the parallel `clone` lifecycle with
its own default action and hooks.

---

## 8. `project` crate API and CLI contract

### 8.1 The crate API (consumer-driven)

The core exists to be queried by scripts/other tools. Confirmed consumer pattern:
a caller supplies branch (and other Profile axes) and asks for resolved info.
Target API surface (to finalize against real call sites):

```rust
let ws = Workspace::load(config_root)?;
ws.projects();                       // iterate all
let p = ws.project("mesa", org)?;    // resolve by name/org

p.repos();                           // all repos of the project
p.build_repo();                      // the `build_in` repo
p.dependency_chain();
p.git_state();                       // branch, commit, dirty, submodules
p.build_dir(&profile);               // Profile-resolved
p.resolve("{{ ... }}", &profile);    // expose the resolver to callers
p.validate();                        // structural issues
```

`ResolvedProject` is the serializable snapshot the core can hand back. The
concrete *output format* of `info` is deferred (see §10) — `--json` is rejected
as the answer; the format is to be designed later.

### 8.2 CLI contract

- **`project info [<name>]`** — no name → list summary; with name → details;
  optional profile flags add resolved build/install dirs. Pure read, zero build
  planning. (Subsumes the old `list`. Output format deferred, §10.)
- **`project info --check [<name>]`** — config-legality validation (required
  fields, valid build system, dependency/preset/inheritance cycles, template
  resolvability, toolchain references exist). No name → check everything (CI use).
  Kept as a `--check` mode of `info` rather than a fourth verb, to preserve the
  three-verb shape; a standalone `project validate` is an equally acceptable
  alternative if preferred.
- **`project build <name>`** — supplies a full Profile (presets, build_type,
  toolchain, generator, mode: auto/config-only/build-only/reconfig, install). Runs
  Phase 1 + Phase 2 + execution in the `build_in` repo. Honors that repo's branch
  strategy (worktree or in-place dance).
- **`project update [<name>] [--all]`** — the §7 lifecycle over all of the
  project's repos.

Global `-v/--verbose` and `-n/--dry-run` are inherited from the `wf` process
layer.

---

## 9. Configuration topology

Configuration is **content-addressed, not path-addressed**: users may store
config files anywhere, and each file declares what it is via its TOML sections.
There is no required directory layout.

### 9.1 Single config-root, resolved

There is exactly **one** config-root (no multi-directory overlay — the old
`BUILDER_CONFIG_DIR` + repeated `-C` layering is gone). It is resolved in order:

```
environment variable  >  --config CLI flag  >  default ($PWD/project)
```

(Env beats CLI, matching the codebase's established "env is the deliberate,
ephemeral override" philosophy in `resolver.rs`.) The default is the `project/`
directory under the current working directory, which also bounds the scan below.

### 9.2 Recursive scan, routing by section

The root is scanned recursively for `*.toml`. Every file is loaded and its
**top-level sections are routed** — a single file may freely mix sections, and
files are merged across the whole tree:

| Section(s) | Meaning |
|---|---|
| `[toolchains.*]` | System-default toolchains (§5.5) |
| `[repos.*]` + `[project]` | One project unit — its repo(s) (git identity, `kind`/`path`/`anchor`) plus build config and `build_in`. A `repos.main` is required. Files merge them; the **concepts stay distinct** (§4). One project : N repos. |
| `[org] name = "..."` + `[org.presets.*]` | An org: a namespace label plus org-level presets (org has nothing else, for now). |
| `[project.presets.*]`, `[repos.<name>.presets.*]` | Project- and repo-level presets, co-located in the project file. |

`org` is always **explicit** (`[org] name` to declare, `project.org` to join) —
never inferred from path, since placement is arbitrary.

### 9.3 Example

```toml
# anywhere under the root, e.g. project/mesa/lavapipe.toml
[project]
org             = "mesa"
build_in        = "lvp"           # focus: the lavapipe subtree
build_system    = "meson"
toolchain       = "clang"
default_presets = ["warnings"]

[repos.main]                       # REQUIRED root — the mesa clone
path        = "~/src/mesa"         # clone destination
main_branch = "main"
[repos.main.remotes]
origin   = "git@github.com:me/mesa.git"
upstream = "https://gitlab.freedesktop.org/mesa/mesa.git"

[repos.lvp]
kind   = "subtree"                 # shares main's git, no own main_branch
path   = "src/gallium/frontends/lavapipe"   # relative to repos.main
anchor = "main"                    # lavapipe builds via the mesa root

[project.presets.debug]            # project-level
definitions = { buildtype = "debug" }
[repos.lvp.presets.debug]          # repo-level (build_in = lvp), most specific
definitions = { b_sanitize = "address" }
```

Org-level presets live in a separate file declaring the org:

```toml
# project/mesa/_org.toml
[org]
name = "mesa"
[org.presets.debug]                # org-level base
definitions = { werror = false }
```

Building `lavapipe --preset debug` yields a `debug` that is the merge of all
three levels (§5.6), with repo-level keys winning. A cross-org reference such as
`extends = ["llvm/base"]` reaches another org's presets.

---

## 10. Open questions / future

- **[open]** Output format of `info` (`--json` rejected; format TBD) (§8).
- **future** 5-layer identity resolution for stacked-diffs / branchless (§6.4).
- **future** Which build systems ship in v1 (cmake/meson/cargo confirmed real;
  bazel/make pending need).
- **future** Submodule + worktree co-existence details (§3.4 — supported, details
  at implementation time).
- **provisional** Repo/Project separation itself — adopted now, may be
  redesigned after more use.
