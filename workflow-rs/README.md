# `wf`

A single binary that collects my personal workflow tools behind one command
tree. The point of a collection (rather than a directory of loose scripts) is a
shared core, consistent flags, and one thing to build and put on `$PATH`.

The collection grows one subcommand at a time, and this repository only ever
contains what is actually finished. Today that is:

| Command | Purpose |
|---|---|
| [`wf transcrypt`](docs/transcrypt.md) | Transparent file encryption wired into git's clean/smudge filters |

When the next tool lands it gets a row here and a module under `src/cmd/`; there
is no plugin system to learn and nothing is carried around for commands that
don't exist yet.

## Building

```sh
cargo build --release   # binary at target/release/wf
cargo test
```

## Invocation forms

Any sub-tool `foo` can be called several equivalent ways:

```sh
wf foo ...     # umbrella form
wf-foo ...     # direct form (a symlink to wf)
wf.foo ...     # same thing with a dot
foo ...        # or a bare name, your choice
```

The direct forms are just a symlink whose name `wf` reads from `argv[0]` and
treats as the subcommand — so it's the same binary, no second process, no extra
copy on disk. A leading `wf-` or `wf.` is stripped; pick whichever name you like
at install time:

```sh
ln -s wf ~/.local/bin/wf.foo
```

A new command earns its direct forms automatically; nothing to register.

## Global flags

| Flag | Meaning |
|---|---|
| `-v`, `--verbose` | Show the underlying git commands as they run |
| `-n`, `--dry-run` | Print mutating commands instead of running them (read-only queries still run) |

`-n` has no visible effect on `transcrypt`, which only ever reads — it lives at
the top level because it is part of the contract future, state-changing commands
inherit from the process layer.

## Design notes

The reasoning behind the shared primitives — why git is driven through the CLI,
why dry-run is shaped the way it is, why the crypto packet format is frozen —
lives in [`docs/core.md`](docs/core.md), and in the module headers themselves.
The comments throughout aim to explain *why* a thing is the way it is; the code
is left to explain *what* it does.
