# git-stack Traversal Behavior

This document is the authoritative reference for **how `git stack sync` and
`git stack anno` decide which branches to process**, both in default mode (the
current branch) and in `--all` mode.  Its primary audience is maintainers who
need to change or debug the traversal logic.

---

## Key Concepts

### The Machete Tree

`.git/machete` records a **dependency forest**: each branch is a node, and its
parent is the branch it should be merged into.  The file allows arbitrary
branching (a.k.a. "tree stacks"), not just linear chains.

```
main
    A          ← single child: linear
        B      ← fork-point (children: C, D)
            C  ← leaf
            D  ← single child: linear
                F  ← leaf
```

### Definitions

| Term | Meaning |
|---|---|
| **fork-point** | A node with `len(children) >= 2` |
| **linear node** | A node with `len(children) <= 1` (leaf or single-child) |
| **ancestors** | The chain from the root down to (but not including) the node itself |
| **subtree** | The node plus all its descendants (DFS pre-order) |
| **linear stack** | `ancestors(N) + [N] + first-child-chain → leaf/next-fork` |

---

## `sync` Traversal

### Default mode (no `--all`, current branch = N)

The decision is based on whether **N itself** is a fork-point:

| Condition | Scope | Rationale |
|---|---|---|
| `len(N.children) >= 2` (fork-point) | `ancestors(N)` + entire `subtree(N)` | N is the root of a branching structure.  "I'm syncing the whole tree I manage." |
| `len(N.children) <= 1` (linear) | `get_linear_stack(N)` — the primary linear path | N is one line of work.  Sibling branches belong to someone else's context. |

**Fork-point example** (standing at B):
```
# Branches pushed:  main(skip, stack_base), A, B, C, D, F
# Printed:  "Fork-point detected: syncing full subtree rooted at 'B' (5 branches)"
```

**Linear example** (standing at D):
```
# Branches pushed:  A, B, D, F   (main skipped as stack_base)
# Printed:  "Limiting sync to linear stack: ['main', 'A', 'B', 'D', 'F']"
# C is NOT pushed — it is a sibling of D and belongs to D's linear context only
```

> **Note:** `stack_base` (usually `main`) is always excluded from push/PR tasks
> via the `is_root_special` guard, regardless of traversal mode.

### `--all` mode

A full DFS over every root in the machete file.  Every branch in every tree is
pushed and has its PR created/updated.  No filtering is applied.

---

## `anno` Traversal

### Default mode (no `--all`, current branch = N)

Target selection follows the same fork-point rule as `sync`:

| Condition | Branches annotated |
|---|---|
| Fork-point | `ancestors(N)` + entire `subtree(N)` |
| Linear | `get_linear_stack(N)` |

However, **the content** of each PR's description is more nuanced than sync
(which only needs a set of branches).  Each PR gets one or more
`### Stack List` sections, all wrapped in a single generated block
(`STACK_HEADER` … `STACK_FOOTER`).

### Annotation block generation (`get_anno_blocks`)

For a given node N, the blocks are computed as follows.

Let `prefix = ancestors(N) + [N]`.

**`path_to_next_fork_or_leaf(start)`** is a helper that walks from `start`
linearly (following the single child at each step) until it encounters either a
fork-point or a leaf, **including that stopping node**.

```
path(C)  where C is leaf               → [C]
path(D)  where D→F(leaf)               → [D, F]
path(B)  where B is fork-point         → [B]        # stops immediately
path(A)  where A→B(fork-point)         → [A, B]
```

**Block generation rules:**

```
if N is fork-point (children >= 2):
    one block per child Ci:
        block_i = prefix + path_to_next_fork_or_leaf(Ci)

if N has exactly one child:
    one block:
        block = prefix + path_to_next_fork_or_leaf(N.children[0])

if N is a leaf:
    one block:
        block = prefix
```

**Why stop at the next fork-point?**
A nested fork-point generates its own multi-block description.  Duplicating its
entire subtree in the parent's description would be redundant and confusing to
reviewers.

### Example: from the sample tree

```
main -> A -> B(fork) -> C(fork) -> E
                                -> G
                     -> D -> F
```

Annotations generated when standing at each node:

| Node | Blocks (branch names inside each block) |
|---|---|
| `main` | `[main, A, B]` — single block; A→B stops at B (fork) |
| `A` | `[main, A, B]` — same as above |
| `B` | `[main,A,B, C]`  ·  `[main,A,B, D,F]` — fork: one block per child, each stops at next fork (C) or leaf (F) |
| `C` | `[main,A,B,C, E]`  ·  `[main,A,B,C, G]` — fork: two leaf blocks |
| `D` | `[main,A,B,D,F]` — linear to leaf |
| `E` | `[main,A,B,C,E]` — leaf |
| `F` | `[main,A,B,D,F]` — leaf |
| `G` | `[main,A,B,C,G]` — leaf |

### Rendered output (B's PR description, two Stack List blocks)

```markdown
<!-- start git-stack-sync generated -->

### Stack List

  * [1/4] PR #10
    `main` ← `A`
  * [2/4] PR #11
    `A` ← `B`  ← (THIS PR would be starred here for B)
  * [3/4] PR #12
    `B` ← `C`

### Stack List

  * [1/5] PR #10
    `main` ← `A`
  * [2/5] PR #11
    `A` ← `B`  ← (THIS PR would be starred here for B)
  * [3/5] PR #13
    `B` ← `D`
  * [4/5] PR #14
    `D` ← `F`

<!-- end git-stack-sync generated -->
```

Reviewers of B's PR can see both downstream paths immediately.

### `--all` mode

`targets = list(nodes.values())` — every node in all trees.  Each node's
description still follows `get_anno_blocks` (multi-block for fork-points).

---

## Code Locations

| Concept | Location |
|---|---|
| `get_ancestors` | `src/machete.py` |
| `get_subtree_nodes` | `src/machete.py` |
| `_path_to_next_fork_or_leaf` | `src/machete.py` (private) |
| `get_anno_blocks` | `src/machete.py` |
| `format_stack_markdown` (with `include_wrapper=`) | `src/machete.py` |
| Sync traversal decision | `src/sync.py` → `StackSyncer._collect_tasks` |
| Anno traversal + block rendering | `src/anno.py` → `annotate_stack` (section 3b) |
| Tests for tree helpers | `tests/test_machete.py` → `TestGetAncestors`, `TestGetSubtreeNodes`, `TestGetAnnoBlocks` |

---

## Invariants to maintain

1. **sync and anno target-set rules must stay in sync.**  If you change the
   fork-point threshold in one, change it in the other.
2. `get_anno_blocks` must never expand a nested fork-point (the "stop at next
   fork" rule).  Doing so would cause exponential block growth.
3. `STACK_HEADER` / `STACK_FOOTER` must appear exactly **once** per PR
   description, wrapping *all* blocks.  `strip_existing_stack_block` relies on
   this single-pair invariant.
4. The `stack_base` node (usually `main`) is always excluded from push/PR tasks
   in sync but is included in annotation blocks so reviewers see the full chain.
