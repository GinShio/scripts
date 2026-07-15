# `wits project` — Reference

The exhaustive reference: every configuration key, every CLI flag, the template
language, and the resolution rules. For a gentle introduction read
[`../project.md`](../project.md); for rationale read [`design.md`](design.md).

> Status: implemented (v1). This documents the contract the code upholds.

---

## 1. CLI

```
project [<name|path>] [--check] [--focus <repo>] [profile flags]
project context create [<name|path>] --branch <X> [--focus <repo>]
project context prune  [<name|path>] --branch <X> [--force]
build   [<name|path>] [--focus <repo>] [profile flags] [build options]
update  [<name|path>]

# Machine-readable queries — one value, one line, for scripts and git hooks.
project main-branch [<name|path>]
project build-dir   [<name|path>] [--branch <X>]
project install-dir [<name|path>] [--branch <X>]
project source-dir  [<name|path>] [--branch <X>]
project work-dir    [<name|path>] [--branch <X>]
```

The four `*-dir` queries resolve the same build [plan](#5-resolution-pipeline)
as `build`/`info` and print one of its paths — `build_dir`, `install_dir`,
`source_dir`, or `work.dir` respectively (`build-dir`/`install-dir` error when
the project declares no such template; `source-dir`/`work-dir` are always
resolvable). The branch defaults to the anchored repo's current one. This is how
a checkout hook points `compile_commands.json` at the active build, or a script
`cd`s into a branch's `work.dir`.

Each verb's positional is a **name or a path**, mutually exclusive:

- **path** if the token is `.`/`..` or begins with `.`, `/`, or `~`. It may point
  *inside* a checkout; the owning project is found by deepest-prefix match
  (`project_for_path`). Shells expand `~` and leave `./` literal, so in practice
  the classifier keys on a leading `.` or `/`.
- **name** otherwise — a bare name or a fully-qualified `org/name`. A bare name
  ambiguous across organisations is a hard error asking you to qualify it. There
  is no `--org` flag.
- **omitted**: `info` covers every project; `build`/`update`/`context` operate on
  the project owning the current directory (a hard error if none does).

### 1.1 Global flags

`-v/--verbose` and `-n/--dry-run` are inherited from the `wits` process layer and
described in [`../project.md`](../project.md) and the top-level README.

### 1.2 Profile flags (affect resolution — build & info)

These set the `Profile` axes and therefore change how paths (`work.dir`,
`build_dir`, `install_dir`) resolve.

| Flag | Alias | Meaning | Default |
|---|---|---|---|
| `--branch <X>` | `-b` | Target branch (the build identity). | focus repo's current branch |
| `--build-type <T>` | `-B` | Build type (`debug`, `release`, `debugoptimized`, …). | the config's default |
| `--toolchain <N>` | `-T` | Select a declared toolchain. | selection chain (§5) |
| `--generator <G>` | `-G` | Build-system generator (e.g. `Ninja`). | the project's `generator` |
| `--preset <P>` | `-p` | Apply a preset; repeatable; accepts `org/preset`. | — |
| `--focus <repo>` | | Override which repo is the build focus. | `project.focus` |

### 1.3 Build options (affect command steps only)

| Flag | Alias | Meaning |
|---|---|---|
| `--config-only` | | Configure only; do not compile. |
| `--build-only` | | Compile only; assume already configured (errors if not). |
| `--reconfig` | | Delete the build dir and configure fresh. |
| `--install` | | Add an install step after building. |
| `--install-dir <DIR>` | | Override the resolved `install_dir` prefix (the backend's install-prefix, e.g. cmake's `CMAKE_INSTALL_PREFIX`). Affects configure as well as install. |
| `--uninstall` | | Reverse an install (backend-driven; see §7.3). Mutually exclusive with a build. |
| `--target <T>` | `-t` | Build a specific target (where the backend supports it). |
| `--extra-config-args <A>…` | `-Xconfig,<arg>` | Raw args appended to the configure command, verbatim. |
| `--extra-build-args <A>…` | `-Xbuild,<arg>` | Raw args appended to the build command, verbatim. |
| `--extra-install-args <A>…` | `-Xinstall,<arg>` | Raw args appended to the install command, verbatim. |

Extra args are applied **last, at the highest priority**, and are never
interpreted by the tool.

Modes are mutually exclusive; the default is `auto` (configure if needed, then
build).

### 1.4 `project` (describe / validate)

`project` with no subcommand is the read command:

- No positional: a one-line summary of every project.
- A name or path (§1): full details for that project, including each repo's
  branch/commit and any worktrees. With profile flags, resolved
  `work.dir`/`build_dir`/`install_dir` are shown; without them, the raw templates.
- `--check`: validate configuration legality (see §8). No positional validates
  everything (CI use); a name/path validates one.

### 1.5 `context`

- `create <name> --branch X`: materialise branch X's build context. Worktree
  strategy → create the worktree (idempotent; errors if X is checked out
  elsewhere). In-place strategy → no-op.
- `prune <name> --branch X`: tear down branch X's build context — remove the
  worktree (if any) and delete `build_dir`. Never touches `install_dir`. `--force`
  removes a dirty worktree.

---

## 2. Configuration topology

- **Config root** (one only), resolved highest-first:
  `$WITS_PROJECT_CONFIG` → `$XDG_CONFIG_HOME/wits/project` → `$HOME/.wits/project`.
- The root is scanned recursively for `*.toml` at load time. A file's top-level
  sections decide what it contributes; a file may mix sections.
- A file with `[project]` (and a required `[repos.main]`) **is one project**. The
  same `(org, name)` in two files is a hard error.
- `[toolchains.*]` and `[org]` + `[org.presets.*]` are additive registries merged
  across the whole tree.
- Organisations are always explicit: `[org] name = "…"` declares one,
  `project.org` joins it. Never inferred from the file path.

### 2.1 Org palette (`[org.environment]` and `[org.definitions]`)

An org may declare shared value tables that projects in the org can reference:

```toml
[org]
name = "acme"

[org.environment]
REGISTRY = "registry.acme.example"

[org.definitions]
ACME_VERSION = 3
```

These are exposed as `org.environment.*` and `org.definitions.*` in the template
context (§6.3). They are a **referenceable palette, not an applied layer**: values
do NOT flow automatically into any build's environment or definitions — a template
must reference them explicitly:

```toml
[project.environment]
PUSH_TO = "{{org.environment.REGISTRY}}"     # pulls from the org palette
```

An unreferenced org palette entry never appears in logical config. This is
deliberate: org presets (§4) — which ARE applied when selected or matched — are
the right mechanism for default build config; the palette is for named shared
constants a project opts into explicitly.

---

## 3. `[project]`

| Key | Type | Required | Meaning |
|---|---|---|---|
| `org` | string | no | Organisation to join (must be declared by some `[org]`). |
| `focus` | string | no | Which `[repos.*]` is the build focus. Default `"main"`. |
| `build_system` | string | when building | `cmake` \| `meson` \| `cargo` (backends shipping in v1). |
| `toolchain` | string | no | Default toolchain name (part of the selection chain, §5). |
| `generator` | string | no | Build-system generator (e.g. `Ninja`). |
| `build_dir` | template | when building | Build directory; see §6 for templating. |
| `install_dir` | template | no | Install prefix; templated. |
| `default_presets` | list\<string\> | no | Presets always applied (§4). |

`[project.environment]` and `[project.definitions]` — templated maps merged at
pipeline layer L1 (§5). `environment` becomes process env for the build;
`definitions` are build-system `-D` parameters. `extra_config_args`,
`extra_build_args`, `extra_install_args` — templated lists appended to the
respective commands.

`[project.presets.<name>]` — project-level presets (§4).

---

## 4. Presets

Declared at three levels:

- `[org.presets.<name>]` — org level (in a file that declares `[org]`).
- `[project.presets.<name>]` — project level.
- `[repos.<focus>.presets.<name>]` — repo level (the focus repo).

### 4.1 Preset keys

| Key | Type | Meaning |
|---|---|---|
| `extends` | string \| list | Inherit other presets; accepts `org/preset`. |
| `applies_when` | table | Structured auto-application match (§4.3). |
| `environment` | table | Templated env vars. |
| `definitions` | table | Templated build definitions. |
| `extra_config_args` | list | Appended to configure. |
| `extra_build_args` | list | Appended to build. |
| `extra_install_args` | list | Appended to install. |

### 4.2 Cross-level merge

A referenced name is the merge of the same-named preset at each level:

- **Maps** (`environment`, `definitions`): merged by key; on conflict the
  **nearest** (repo > project > org) level wins.
- **Lists** (`extra_*_args`): the **nearest** level's list **replaces** the
  others (not appended).

The merged definition's `extends` are then resolved.

### 4.3 `applies_when`

A table over a fixed key set: `build_type`, `toolchain`, `os`, `arch`,
`generator`.

- Multiple keys are AND-ed.
- A key's value is a scalar (equality) or an array (membership / OR).
- Comparison is **case-sensitive**.

A match auto-applies the preset for that build.

### 4.4 Application order

`default_presets` → `applies_when` matches → `--preset` (CLI). The combined list
is de-duplicated by name keeping the **last** position, so an explicitly-passed
preset moves late and wins. CLI `-X`/`--extra-*-args` (pipeline L3) sit above all
presets.

---

## 5. `[toolchains.<name>]`

Toolchains are **100% user-declared** — there are no built-ins. The vocabulary is
aligned with meson's native file. All fields are optional; declare what your
build needs.

### 5.1 Canonical fields (translated to each backend, §7)

| Field | Meaning |
|---|---|
| `cc`, `cxx`, `rustc` | Compilers. |
| `ar`, `nm`, `ranlib`, `strip` | Binutils. |
| `linker` | Linker (e.g. `mold`, `lld`). |
| `launcher` | Compiler launcher (e.g. `ccache`, `sccache`). |
| `c_flags`, `cxx_flags`, `link_flags` | Flag lists. |
| `supports` | Optional list of build systems, used only by `info --check`. |

Each canonical field is translated to a universal environment variable (`CC`,
`CXX`, `AR`, `NM`, `RANLIB`, `STRIP`, `CFLAGS`, `CXXFLAGS`, `LDFLAGS`, `RUSTC`)
plus a backend-native definition where one exists — see §7.

### 5.2 Pass-through blocks (not translated)

- `[toolchains.<name>.environment]` — env vars applied verbatim.
- `[toolchains.<name>.definitions]` — definitions applied verbatim.

### 5.3 Selection chain

```
env  >  --toolchain  >  project/repo `toolchain` field  >  [toolchains] entry
```

The toolchain *name* is always selected (path templates depend on it). Its
env/definitions **injection** is skipped in `auto`/`build-only` mode when the
build dir is already configured and no toolchain was explicitly requested.

---

## 6. Templates

Config values are templated. Config format is TOML only.

### 6.1 `{{ path }}` substitution

Dotted lookup over the context (§6.3): tables by key, arrays by integer index. A
value that is a single whole-string placeholder returns the **typed** value (a
list or integer survives); an embedded placeholder is stringified (`true`/`false`
lowercase, integers decimal). Resolution is lazy and recursive with cycle
detection.

### 6.2 `[[ expr ]]` expressions

A minimal numeric expression, e.g. `LINK_JOBS = "[[ max(1, system.memory.total_gb // 4) ]]"`.

- Operators: `+ - * / // %` over int/float; comparisons `== != < <= > >=`.
- Functions: `min`, `max`, `int`, `float`, `str`, `bool`.
- **Not** supported: `**`, bitwise ops, `and`/`or`/`not`, ternary, arbitrary
  names, list/dict literals. (Conditions are `applies_when`, §4.3.)

### 6.3 Context variables

```
project.{ name, org, focus }
repo.*                     # the *current* repo (focus repo in project scope;
                           #   the repo itself in a repo-scoped field like a hook)
  { name, path, kind, main_branch, anchor, origin, upstream, mirrors }
repos.<name>.*             # any repo by explicit name; same fields as repo.*
org.environment.<K>        # org palette entry (see §2.1); referenceable, not auto-applied
org.definitions.<K>        # org palette entry (see §2.1); referenceable, not auto-applied
work.dir                   # effective checkout dir for this build (§9)
branch.{ raw, slug }       # raw = branch name; slug = filesystem-sanitised
build_type
toolchain.{ name, cc, cxx, rustc, ar, nm, ranlib, strip,
            linker, launcher, c_flags, cxx_flags, link_flags }
generator
system.{ os, arch, memory.total_gb, cpu.count }
env.*                      # process environment
```

- `repo` is a **relative** alias for the repo being resolved; use `repos.<name>`
  to reference any other repo.
- There is no bare `{{branch}}`; use `{{branch.raw}}` or `{{branch.slug}}`.
- `repo.upstream` falls back to `repo.origin` when no upstream is declared.
- `org.environment.*` / `org.definitions.*` are available in project scope and in
  repo-scoped fields (hooks, `worktree_dir`). Only accessible when `project.org`
  is set and the org declares the key; references to undeclared keys are hard errors.

### 6.4 Errors

Every failure is hard: unknown path, cycle, type mismatch, division by zero. The
context is always fully populated, so a missing path always means a real mistake.

---

## 7. Backends

`build_system` selects a backend. A backend does three things: translates the
selected toolchain's canonical fields to native form, emits the command steps for
a mode, and detects prior configuration.

### 7.1 Canonical-field translation

| Canonical | cmake | meson | cargo |
|---|---|---|---|
| `cc` / `cxx` | `CMAKE_C/CXX_COMPILER` | `CC`/`CXX` (env / native file) | `CC`/`CXX` env |
| `rustc` | — | — | `RUSTC` |
| `ar`/`ranlib`/`strip`/`nm` | `CMAKE_AR`/`CMAKE_RANLIB`/… | native file / env | env |
| `linker` | `CMAKE_LINKER` / `-fuse-ld` | `CC_LD`/`CXX_LD` | `CARGO_TARGET_*_LINKER` |
| `launcher` | `CMAKE_*_COMPILER_LAUNCHER` | prefix on `CC`/`CXX` | `RUSTC_WRAPPER` |
| `c_flags`/`cxx_flags`/`link_flags` | `CMAKE_C/CXX_FLAGS` / linker flags | `CFLAGS`/`CXXFLAGS`/`LDFLAGS` | `CFLAGS` / `RUSTFLAGS` |

For meson and cargo, each canonical field is *also* exported as its universal env
var (`CC`, `CXX`, `AR`, `CFLAGS`, …); **cmake is the exception** — it is
configured entirely through `-D` definitions and is not given these environment
variables, which it does not need and which can conflict with its cached
compiler. This translation runs at pipeline layer L0, so an explicit preset or
CLI override of the same key wins.

Multi-config cmake generators (Ninja Multi-Config, Visual Studio, Xcode) are
handled correctly: `CMAKE_BUILD_TYPE` is *not* set at configure, and the build
type is selected at build/install time with `--config`.

### 7.2 `is_configured`

- cmake: `CMakeCache.txt` present in the build dir.
- meson: `meson-private/coredata.dat` present.
- cargo: not applicable.

### 7.3 Modes

`auto` | `config-only` | `build-only` | `reconfig` | `uninstall`. `--install`
adds an install step to a build. `uninstall` is backend-driven — meson `ninja -C
<build> uninstall`, cmake via `install_manifest.txt`, cargo unsupported — never a
recursive delete, because an install prefix may be shared.

---

## 8. `info --check` validation

Reports (does not fix): required fields present (`repos.main`, `main_branch` for
own-git repos, `build_dir`/`build_system` when building); preset inheritance and
template reference cycles; template resolvability against a representative
context; referenced toolchains exist; and, when a toolchain declares `supports`,
that it covers the project's build system. No `<name>` checks every project.

Whether the declared `build_system` actually has a backend is **not** checked
here — that is reported by `wits build` at run time, since the read-only core
deliberately knows nothing of which build systems are implemented.

---

## 9. Repos, branches, and build contexts

### 9.1 `[repos.<name>]`

| Key | Type | Required | Meaning |
|---|---|---|---|
| `path` | **template** | yes | On-disk location (root/standalone) or subpath relative to `repos.main` (nested). Resolved against a Profile-free context: `project.name`, `project.org`, `env.*`, `system.*` — no `repos.*` (would be circular). Nesting + `main_branch` determine the inferred kind (below). |
| `main_branch` | string | own-git repos | The branch `update` fast-forwards. Not allowed for `subtree`. |
| `anchor` | string | no | Repo whose `path` is this build's source/base; unset → self. |
| `source_dir` | template | no | Where the backend configures from (the top-level `CMakeLists.txt`/`meson.build`/…) when it is not the checkout root. Read from the build repo; defaults to `work.dir`. E.g. `"{{work.dir}}/src"`. Only the configure source changes — `work.dir` still anchors `build_dir`/`install_dir` and branch identity. |
| `branch_strategy` | string | no | `in-place` (default) \| `worktree`. |
| `worktree_dir` | template | worktree strategy | Where a branch's worktree lives. |

**Kind is inferred, not declared**: a non-nested `path` → `standalone`; a nested
`path` with `main_branch` → `submodule`; nested without `main_branch` → `subtree`.
`repos.main` is always standalone.

`[repos.<name>.remotes]` — `origin` (string, the push target / fork), `upstream`
(string, the **sync source**), `mirrors` (list of extra push URLs on origin).
The **sync source** = `upstream` if declared, else `origin`; it is what `clone`
and `update` fetch from and fast-forward `main` against. When an `upstream` is
declared, `origin` is **never fetched or cloned** — so a fork that does not yet
exist on the server is fine (it is only added as a push target). Reconciliation
is additive only: missing remotes/mirror push-URLs are added; existing URLs are
never modified or removed; unmentioned remotes are untouched.

`[repos.<name>.hooks]` — inline `sh -c` command strings, templated. Phases:
`clone` / `post_clone` and `pre_update` / `update` / `post_update`. (The clone
phase has no `pre` hook — the repo does not exist yet.) The bare phase name
(`clone`, `update`) overrides that phase's default action; `pre_`/`post_` add
hooks around it.

**Hook cwd by phase**: a `clone` override runs in the **current working
directory** (the repo's `path` does not exist yet, and `git clone` creates the
destination itself); `post_clone` runs in the **repo's `path`** (now exists after
cloning); `pre_update`, the `update` override, and `post_update` all run in the
**repo's `path`**.

A non-zero exit fails fast (§10).

`[repos.<name>.presets.<preset>]` — repo-level presets (§4).

### 9.2 `{{work.dir}}` resolution

`work.dir` is the **anchor** (build repo)'s effective checkout — where the build
sources from. The **focus** is the repo switched to the target branch within it
(its own git, or the git it shares when the focus is a subtree).

- **in-place**: `work.dir` = the build repo's `path`. Switching the focus to a
  non-current branch stashes, switches, builds, then always restores (branch,
  stash, and the focus's submodules) on any exit.
- **worktree**: `work.dir` = the resolved `worktree_dir` for the target branch.
  It must already exist (`project context create`); `build` never creates it.

### 9.3 Branch identity

The identity is the branch name of the nearest own-git repo in the
`focus → anchor` chain. A detached HEAD is unsupported. `branch.slug` replaces
every character outside `[A-Za-z0-9._-]` (including `/`) with `_`.

---

## 10. `update` / `clone`

For each repo (parents before nested; subtrees do no git work):

The **sync source** = `upstream` if declared, else `origin` (§9.1).

- **Missing path → clone**: action (default: `git clone --origin <sync>` from the
  sync source, set up remotes, checkout `main_branch`, `submodule update --init
  --recursive`; a `clone` override runs in the current working directory) →
  `post_clone` (cwd = repo path, now exists). `git clone` creates the destination
  (and any leading directories) itself, so nothing is pre-created. Cloning names
  the fetched remote after the sync source, so tracking an `upstream` leaves the
  `origin` name free for a fork (added, not fetched, by remote reconciliation).
- **Existing → update**: ensure remotes (additive) → `pre_update` → action →
  `post_update` (all with cwd = repo path).

Default update action:

- On `main_branch`: `git fetch <sync>` then `git merge --ff-only
  <sync>/<main_branch>`.
- Otherwise: `git fetch <sync> <main_branch>:<main_branch>` — a ref-only
  fast-forward that does not check out, does not touch the working tree, and does
  not expand a sparse checkout.
- Declared submodule repos advance via their own lifecycle; undeclared nested
  submodules are refreshed with `git submodule update --recursive -- <materialised
  paths>` (no `--init`; `--init` happens only on clone or worktree creation).

Failure is fail-fast: a non-zero hook/action stops the operation, an RAII guard
restores the original branch (and pops any stash), a log line is written,
remaining repos are skipped, and the process exits non-zero.

---

## 11. Crate API (read-only)

```rust
let ws = Workspace::load(config_root)?;
ws.projects();                        // iterate
ws.project("mesa", org)?;             // by name / org
ws.project_for_path(&path);           // which project owns this checkout

let p = ws.project(...)?;
p.repos();  p.build_repo();
p.main_branch();
p.git_state();                        // branch, commit, dirty, submodules
p.work_dir(&profile);
p.build_dir(&profile);  p.install_dir(&profile);
p.resolve("{{ ... }}", &profile);
p.validate();
```

The core resolves paths but never destroys them; the only side-effecting entry
points are the `build`, `update`, and `context` actions.
