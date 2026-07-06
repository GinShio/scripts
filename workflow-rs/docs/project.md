# `wf project`

Build, update, and introspect the source projects you work on, from one
declarative registry that knows *what each project is* — where its repos live,
which branches, which toolchain, how to build it — and drives cmake / meson /
cargo on your behalf without you re-typing the same flags.

Three commands share that registry: **`wf project`** describes and validates
(read-only), **`wf build`** configures and builds, and **`wf update`** refreshes
git. (`wf project context` manages per-branch worktrees.) This page is the
**usage guide** for all of them.

For
the exhaustive list of every config key and every flag, see
[`project/reference.md`](project/reference.md). For *why* the tool is shaped this
way, see [`project/design.md`](project/design.md).

> Status: implemented (v1). This guide describes the behaviour the tool provides.

---

## The mental model in one minute

- A **project** is a buildable unit described by one TOML file. It owns one or
  more **repos** (git checkouts); one of them, `repos.main`, is always required.
- A **toolchain** is a named set of compilers/tools you declare once and select
  per build. The tool ships no built-in toolchains — you declare your own.
- A **preset** is a reusable bundle of build settings you can layer on.
- A **build context** is where a branch actually builds: either a git *worktree*
  or an in-place build directory, your choice per repo.

Everything is content-addressed: config files can live anywhere under the config
root and declare what they are by their sections. There is no required layout.

---

## Where config lives

`project` reads one config root, resolved in this order:

1. `$WITS_PROJECT_CONFIG` (environment)
2. `$XDG_CONFIG_HOME/wits/project`
3. `$HOME/.wits/project`

Drop `*.toml` files anywhere under it. A file with a `[project]` section is a
project; files with `[toolchains.*]` or `[org]` sections contribute to shared
registries and are merged across the whole tree.

---

## Your first project

Create `~/.wits/project/hello.toml`:

```toml
[project]
build_system = "cmake"
toolchain    = "clang"

[repos.main]
path        = "~/src/hello"
main_branch = "main"
[repos.main.remotes]
origin = "git@github.com:me/hello.git"

# Where the build lands. {{work.dir}} is this build's checkout dir;
# keying by branch means switching branches never clobbers another build.
build_dir   = "{{work.dir}}/_build/{{toolchain.name}}/{{build_type}}"
install_dir = "{{work.dir}}/_install/{{build_type}}"
```

Declare the `clang` toolchain once (e.g. `~/.wits/project/toolchains.toml`):

```toml
[toolchains.clang]
cc       = "clang"
cxx      = "clang++"
ar       = "llvm-ar"
linker   = "mold"
launcher = "ccache"
supports = ["cmake", "meson"]
```

Now:

```sh
update  hello      # clone if missing, otherwise refresh git
build   hello      # configure + build with clang, debug by default
project hello      # what is it, where does it build, what branch is it on
```

`build` translates the toolchain into cmake's native flags for you — you never
write `CMAKE_C_COMPILER` yourself.

---

## Building: types, toolchains, presets, modes

```sh
build hello -B release                 # build type (lowercase, meson-aligned)
build hello -T gcc                     # a different declared toolchain
build hello -p asan -p lto             # apply presets (repeatable)
build hello --config-only              # (re)configure, don't compile
build hello --build-only               # compile, assume already configured
build hello --reconfig                 # wipe the build dir and configure fresh
build hello --install                  # add an install step
build hello --uninstall                # reverse an install (backend-driven)
```

Pass raw, untouched flags straight through to the underlying tool when you need
something one-off — these are applied last, at the highest priority:

```sh
build hello --extra-config-args -DFOO=BAR --extra-config-args -DBAZ=1
build hello -Xconfig,-DFOO=BAR         # short form, scope = config|build|install
build hello -Xbuild,-j8
```

The tool does not interpret these — `-DFOO=BAR` is handed to cmake verbatim.

### Choosing a toolchain without editing config

Selection order is `env → --toolchain → the project's toolchain field →
[toolchains]`. Environment wins, so you can flip toolchain for a run:

```sh
WITS_PROJECT_TOOLCHAIN=gcc build hello
```

(Selecting a toolchain always happens so paths like `.../{{toolchain.name}}/...`
resolve; in `auto`/`build-only` mode an already-configured build dir is trusted
and not reconfigured just because you re-ran `build`.)

---

## Presets

A preset bundles environment, definitions, and extra args, and can inherit and
auto-apply itself:

```toml
[project.presets.debug]
definitions = { ENABLE_ASSERTS = true, ENABLE_TESTS = true }

[project.presets.asan]
extends      = ["debug"]
applies_when = { build_type = "debug", toolchain = ["clang", "clang-cl"] }
environment  = { ASAN_OPTIONS = "detect_leaks=1" }
definitions  = { SANITIZER = "address" }

[project]
default_presets = ["warnings"]     # always applied
```

- `default_presets` always apply; an `applies_when` match auto-applies; `-p NAME`
  applies explicitly. Explicit wins.
- Presets exist at three levels — `[org.presets.*]`, `[project.presets.*]`,
  `[repos.<focus>.presets.*]` — and a name is the merge of all three, most
  specific winning. Reach another org with `-p llvm/base`.

See the reference for the exact merge and match rules.

---

## Multiple repos: monorepos, submodules, subtrees

A project can own several repos. `repos.main` is the required root; others hang
off it and pick a `focus` for building.

```toml
[project]
focus        = "lvp"           # build the lavapipe component
build_system = "meson"
toolchain    = "clang"

[repos.main]                   # the mesa clone (required root)
path        = "~/src/mesa"
main_branch = "main"
[repos.main.remotes]
origin   = "git@github.com:me/mesa.git"
upstream = "https://gitlab.freedesktop.org/mesa/mesa.git"

[repos.lvp]                    # a subtree (inferred: nested path, no main_branch)
path   = "src/gallium/frontends/lavapipe"   # relative to repos.main → shares mesa's git
anchor = "main"                # build via the mesa root

build_dir = "{{work.dir}}/_build/lvp/{{build_type}}"
```

- `anchor = "main"` means "build from the mesa root" — the configure source is
  mesa, and lavapipe is selected through meson options. `anchor` may point at any
  repo, or be left unset to build a repo on its own.
- The **kind** of each repo is inferred, never declared: a nested path with its
  own `main_branch` is a **submodule**; a nested path without one is a **subtree**;
  a non-nested path is **standalone**. A submodule is cloned through `repos.main`.
- `update` refreshes *every* repo; `build` builds the `focus`; you can switch
  focus for one run with `--focus <repo>` — handy in a large monorepo.

---

## Branches: in-place vs worktrees

Each repo picks how multi-branch work is realised:

```toml
[repos.main]
branch_strategy = "in-place"    # default: one checkout, switched at build time
# or:
branch_strategy = "worktree"
worktree_dir    = "{{repo.path}}.worktrees/{{branch.slug}}"
```

- **in-place**: `build --branch X` stashes, switches to X, builds, then always
  switches back and restores your working tree — even if the build fails.
- **worktree**: each branch gets its own directory, so builds never disturb your
  current checkout and can run in parallel. `build` requires the worktree to
  exist first:

```sh
project context create hello --branch feature-x   # make the worktree
build                  hello --branch feature-x   # build in it
project context prune  hello --branch feature-x   # tear it down when done
```

`context prune` removes the worktree (or, under in-place, just the build dir) and
the build directory — but never your install prefix. It is strategy-transparent,
which is exactly what a "clean up after a deleted branch" git hook wants to call.

---

## Updating

```sh
update hello       # one project's repos
update             # every project
```

`update` is safe by default: if you are on a feature branch, it fast-forwards the
main branch's ref *without* checking it out — nothing is stashed or switched, and
a sparse checkout is never expanded. It also ensures your declared remotes exist
(adding missing ones and mirror push-URLs) but never rewrites URLs you set
yourself.

---

## Inspecting and validating

```sh
project                       # one-line summary of every project
project hello                 # details for one
project hello -b feature-x -B release   # resolved build/install/work dirs for that profile
project --check               # validate every project's config (CI)
project --check hello         # validate one
```

`info` is pure read — it never builds or switches anything.

---

## Running from inside a checkout

Every verb takes a project **name** or a **path**, or nothing at all. A path may
point *inside* a checkout — the owning project is found automatically — and with
no argument the verbs act on the project you are currently standing in:

```sh
cd ~/src/mesa/src/gallium/frontends/lavapipe
build                # builds the project owning this directory
project .            # details for that project
project ~/src/hello  # by path, from anywhere
```

A token starting with `.`, `/`, or `~` is treated as a path; anything else is a
name (`hello` or `mesa/lavapipe`). With no argument, `info` lists every project
while the other verbs use the current directory.

## Global flags

Inherited from `wf` (see the top-level README):

| Flag | Meaning |
|---|---|
| `-v`, `--verbose` | Show the underlying git / build commands as they run |
| `-n`, `--dry-run` | Print mutating commands instead of running them (reads still run) |

---

## Where to go next

- [`project/reference.md`](project/reference.md) — every config key and every CLI
  flag, precisely.
- [`project/design.md`](project/design.md) — the rationale behind every decision
  here.
