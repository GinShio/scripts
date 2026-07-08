# `wits`

A single binary that collects my personal workflow tools behind one command
tree. The point of a collection (rather than a directory of loose scripts) is a
shared library, consistent flags, and one thing to build and put on `$PATH`.

The collection grows one subcommand at a time, and this repository only ever
contains what is actually finished. Today that is:

| Command | Purpose |
|---|---|
| [`wits transcrypt`](docs/transcrypt.md) | Transparent file encryption wired into git's clean/smudge filters |
| [`wits stack`](docs/stack.md) | Manage a stack of branches as a set of merge requests (push, open/retarget MRs, navigation) |
| [`wits project`](docs/project.md) | Describe/validate source projects from one declarative registry (also manages build contexts via `project context`) |
| [`wits build`](docs/project.md) | Configure and build a project on top of that registry (cmake/meson/cargo) |
| [`wits update`](docs/project.md) | Refresh git for every repo of a project |

Built-in commands live in the `wits` binary (a module under
`crates/wits/src/cmd/` plus a match arm). Anything else is a **plugin**: `wits
foo` runs a `wits-foo` executable from your `$PATH`, git-style, so a
domain-specific workflow plugs in without being compiled into `wits`.

## Install

`wits` is a Cargo workspace: the `wits` binary plus a shared `wits-util` library
(and any plugin crates). `cargo install` cannot create the applet symlinks the
dispatch relies on, so use the bundled script:

```sh
./install.sh                 # build --release, install into ~/.local/bin
./install.sh --prefix ~/bin  # somewhere else
./install.sh --dry-run       # show what it would do, change nothing
```

It installs the `wits` binary, a `wits-<sub>` symlink for every built-in, and
any plugin binaries the workspace built. To build without installing: `cargo
build --release` (binary at `target/release/wits`); `cargo test` runs the suite.

## Invocation forms

A built-in `foo` can be called two ways:

```sh
wits foo ...     # umbrella form
wits-foo ...     # direct form (a symlink to wits, created by install.sh)
```

The direct form is a symlink whose name `wits` reads from `argv[0]` and splices
back in as the subcommand — same binary, no second process. Applet names come
straight from the subcommand list, so a new built-in earns its symlink for free.

## Plugins

When `foo` is not a built-in, `wits foo` execs a `wits-foo` executable found on
`$PATH` — the convention git and cargo use. A plugin is therefore any executable
named `wits-<name>`, in any language; an in-tree Rust plugin can additionally
depend on `wits-util` to reuse the process/git/config/template floor rather than
reinventing it. `wits help` lists the built-ins plus the plugins it discovers on
`$PATH`. The full contract is in [docs/plugins.md](docs/plugins.md).

## Global flags

| Flag | Meaning |
|---|---|
| `-v`, `--verbose` | Show the underlying git commands as they run |
| `-n`, `--dry-run` | Print mutating commands instead of running them (read-only queries still run) |

`-n` has no visible effect on `transcrypt`, which only ever reads — it lives at
the top level because it is part of the contract future, state-changing commands
inherit from the process layer.
