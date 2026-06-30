# The shared core

`src/core/` holds the primitives the commands lean on. This document records
*why* each one exists and the one decision in it that isn't obvious — the kind
of thing that is invisible in the code and expensive to rediscover. For the
mechanics, read the module; for the API, read the signatures.

## `process` — running commands, with dry-run baked in

These tools spend most of their time shelling out, and the thing that makes that
fiddly is dry-run. `-n` should suppress anything that *changes* the world, but
the read-only queries that decide what to do next must still run — otherwise a
dry-run collapses into a no-op that reports nothing useful.

That tension is the module's reason to exist. A command is built, and a
read-only query opts out of the dry-run guard with `force_run`. Everything else
is printed instead of executed when `-n` is on. The dry-run preview goes to
stdout while logs go to stderr, so a plan can be captured cleanly.

There are two ways to run. The default captures stdout/stderr — the right thing
for a query whose output we parse. The other inherits the terminal and returns
only an exit code, for a command that *is* an interaction: anything that opens an
editor or drives an interactive rebase must own the terminal, so capturing its
output would break it. Both honour dry-run.

## `git` — driven through the CLI, deliberately

The tempting alternative is libgit2. We don't, and the reason is fidelity rather
than weight. A user's real git behaviour is the sum of their `~/.gitconfig`
includes, conditional includes, credential helpers and SSH setup. libgit2
reimplements a subset and drifts from the CLI in exactly the corners (includes,
helpers) that config resolution depends on. Spawning the same `git` the user's
shell runs means we read precisely what they would, with no second
implementation to keep honest. A process spawn per query is free next to the
work these commands actually do.

The surface grows strictly with what the commands need, and only ever has. It
started as config reads; the stack tool added branch and ref reads, a commit-log
range read (for MR titles), remote-URL reads, and one mutation — a
force-with-lease push. The lease, not a bare force, is the deliberate bit: a
stack is rewritten constantly so non-fast-forward pushes are the norm, but the
lease still refuses to clobber a remote someone else moved.

The larger git-hosting concerns — parsing remote URLs, detecting a forge,
talking to its MR API — deliberately do *not* live in `core`. They sit in
`util/` (`util::remote`, `util::forge`), because they carry real domain logic of
their own and `core` is kept to the floor. See `docs/stack/design.md` for that
layer's shape.

## `cli` — layered config resolution

A setting like the encryption password can live in an environment variable or in
git config, and we want one predictable precedence order with no bootstrap loop
(the resolver can't itself need config to find config).

The subtle part is context isolation. A repository can hold several independent
secret sets — `default`, `prod`, and so on. When a non-default context is active
the resolver refuses to fall back to the bare, context-less key. Falling back
would silently hand a `prod` operation the `default` password and encrypt data
under the wrong key; the bare key is only consulted for the default context.

## `crypto` — authenticated encryption shaped by git filters

Two domain constraints drive everything here.

**Compatibility.** Repositories already hold data encrypted by the earlier
`transcrypt` tool. The packet layout, the algorithm-name spellings, and the
default PBKDF2 iteration count are therefore a frozen wire format: reproduce them
exactly or that data becomes unreadable. This is why a few constants look
arbitrary — they are, and they can't change.

**Determinism.** A clean filter runs on every `git add`. If encrypting unchanged
content produced fresh randomness, git would see the file as modified forever.
So the default mode derives salt and IV from the content itself: same input,
same output, no phantom diffs. The cost is that identical plaintext is
observably identical once encrypted — fine here, and the price of a filter that
doesn't fight git. The derivation also folds in the file path as the AEAD's
additional data, binding a ciphertext to its location so a moved blob fails to
authenticate instead of silently decrypting.
