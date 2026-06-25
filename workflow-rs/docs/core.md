# Core Library (`src/core/`)

The `core` crate contains the foundational building-blocks shared by every
`wf` subcommand.  Each module has a single, well-defined responsibility, and
subcommands depend on it rather than duplicating logic.

---

## Module overview

| Module | Source | Purpose |
|---|---|---|
| `log` | `src/core/log.rs` | Global verbose/dry-run flags and custom `log::Log` implementation |
| `process` | `src/core/process.rs` | Fluent subprocess builder (`Command`) with dry-run interception |
| `git` | `src/core/git.rs` | Pure-CLI Git repository API |
| `config` | `src/core/config.rs` | TOML v1.0 config loading with path resolution and deep merge |
| `crypto` | `src/core/crypto.rs` | AEAD encryption/decryption with SIV and Python-compatible packet format |

---

## `core::log`

### Design

A zero-dependency custom `log::Log` implementation backed by two global
`AtomicBool` values:

| Flag | CLI flag | Default |
|---|---|---|
| `VERBOSE` | `-v / --verbose` | `false` |
| `DRY_RUN` | `-n / --dry-run` | `false` |

Call `log::init(verbose, dry_run)` once in `main()` before any log macros
fire.  Subsequent calls update the atomics (safe to call in tests).

### Log format

```
[LEVEL] (scope) message
```

- `LEVEL` is always uppercase (`DEBUG`, `INFO`, `WARN`, `ERROR`).
- `scope` is the last path segment of `record.target()`, omitted for the root
  crate.
- All output goes to **stderr** except dry-run lines which go to **stdout**
  (so shell scripts can capture "what would run" cleanly).

### Dry-run output

```rust
// Anywhere in the codebase:
wf::core::log::dry_run("git push origin main");
// stdout: [DRY-RUN] git push origin main
```

---

## `core::process`

### `Command` builder

```rust
let result = Command::new("git")
    .arg("rev-parse")
    .arg("HEAD")
    .force_run()        // bypass dry-run for read-only queries
    .exec()?;

println!("{}", result.stdout_trimmed());
```

| Method | Description |
|---|---|
| `arg(s)` / `args(iter)` | Append argument(s) |
| `current_dir(path)` | Set working directory |
| `env(k, v)` | Add environment variable override |
| `force_run()` | Always execute, even during dry-run |
| `exec()` | Capture stdout + stderr, return `CommandResult` |
| `exec_check()` | Same as `exec()` but return `Err` on non-zero exit |
| `stream()` | Inherit terminal stdio (for real-time output) |
| `stream_check()` | Same as `stream()` but return `Err` on non-zero exit |

### Dry-run behaviour

When `is_dry_run()` is `true` and `force_run()` was _not_ called:

1. The command is formatted and printed: `[DRY-RUN] git push origin main`
2. A synthetic `CommandResult { exit_code: 0, stdout: "", stderr: "" }` is
   returned — the real process is **never spawned**.

### Error handling

`ProcessError` has three variants:

| Variant | When |
|---|---|
| `Spawn { program, source }` | The OS rejected the `spawn()` syscall |
| `Failed { cmd, exit_code, stdout, stderr }` | Non-zero exit with `exec_check()` / `stream_check()` |
| `Io(io::Error)` | Other I/O failures |

---

## `core::git`

### `Repository`

```rust
let repo = Repository::new("/path/to/repo");
```

All methods delegate to `git` sub-processes via `core::process::Command`.

#### Configuration

```rust
repo.get_config("transcrypt.password")?   // → Option<String>
repo.set_config("workflow.key", "value")?
repo.unset_config("workflow.key")?
```

#### Status

```rust
repo.head_branch()?       // → Option<String>  (None in detached HEAD)
repo.resolve_commit("HEAD")? // → Option<String>  (None for unborn branches)
repo.is_dirty(true)?      // → bool  (include_untracked = true)
repo.default_branch("origin")? // → Option<String>
```

#### Remotes

```rust
repo.list_remotes()?                                    // → Vec<String>
repo.remote_url("origin")?                              // → Option<String>
repo.remote_urls("origin", /* push */ true)?            // → Vec<String>
repo.add_remote("upstream", "https://…")?
repo.rename_remote("origin", "upstream")?
repo.set_remote_url("origin", "https://…", true, true)? // push + add
```

#### Branches & stash

```rust
repo.create_branch("feature/foo", None)?
repo.checkout("feature/foo", /* create */ false)?
repo.stash(Some("WIP: in-progress"))?  // → bool (was anything stashed?)
repo.stash_pop()?
```

---

## `core::config`

### Path resolution

`resolve_config_path(cli_path: Option<&str>) -> Option<PathBuf>`

| Priority | Source |
|---|---|
| 1 | `cli_path` argument |
| 2 | `WF_CONFIG` environment variable |
| 3 | `wf.toml` in CWD |
| 4 | `.wf.toml` in CWD |

### `ConfigLoader`

```rust
#[derive(Debug, Default, Deserialize)]
struct MyConfig {
    toolchain: Option<String>,
    max_jobs: Option<u32>,
}

let loader = ConfigLoader::new();
let cfg: MyConfig = loader.load(Some("/custom/path.toml"))?;
// or auto-resolve:
let cfg: MyConfig = loader.load(None)?;
```

When the resolved path is a **directory**, all `*.toml` files are parsed in
alphabetical order and deep-merged.  This enables a split-config pattern where
users maintain separate files for different concerns:

```
config/
  00-defaults.toml
  10-toolchains.toml
  20-targets.toml
```

### Deep merge rules

| Type | Behaviour |
|---|---|
| Table/inline-table | Merged key-by-key recursively |
| Array | Overlay replaces base |
| Scalar (string, int, bool, …) | Overlay replaces base |

---

## `core::crypto`

### Packet format

Every encrypted file is base64-encoded.  The raw binary layout is:

```
┌─────────────┬──────────┬──────────┬──────────┬──────────────────┬──────────┐
│ "Salted__"  │  salt    │ "IVed__" │   iv     │   ciphertext     │   tag    │
│   8 bytes   │  8 bytes │  6 bytes │ 12 bytes │    N bytes       │ 16 bytes │
└─────────────┴──────────┴──────────┴──────────┴──────────────────┴──────────┘
```

This format is **byte-for-byte identical** to the Python `transcrypt` and Zig
`wf crypt` implementations, ensuring existing encrypted repositories remain
readable without re-encryption.

### Supported algorithms

| Category | Options | Default |
|---|---|---|
| Cipher | `aes-256-gcm`, `chacha20-poly1305` | `aes-256-gcm` |
| KDF | `pbkdf2`, `argon2id` | `pbkdf2` |
| Hash (for PBKDF2 HMAC and SIV) | `sha256`, `sha384`, `sha512`, `sha3256`, `sha3384`, `sha3512`, `blake2b`, `blake2s` | `sha256` |

### Default iterations

| KDF | Default | Reason |
|---|---|---|
| PBKDF2 | **99 989** | Matches legacy Python transcrypt for backward compatibility |
| Argon2id | **4** (+ 128 MiB memory, 2 lanes) | OWASP-recommended baseline |

### SIV (Synthetic IV) mode

Deterministic encryption is critical for `git clean`/`smudge` filters: the
same plaintext must always encrypt to the same ciphertext so that unchanged
files do not produce spurious diffs.

The SIV construction derives both salt and IV from a hash of the content:

```
algo_params = "{hash}:{cipher}:{iterations}:{kdf}"
              e.g. "sha256:aes-256-gcm:99989:pbkdf2"

H = Hash(
      4BE(len(algo_params)) || algo_params || \x00 ||
      4BE(len(password))    || password    || \x00 ||
      4BE(len(context))     || context     || \x00 ||
      plaintext
    )

iv   = H[0  .. 12]
salt = H[12 .. 20]
```

The `context` (typically the file path) is also used as AEAD **Additional
Data** (AAD).  Decrypting a file with the wrong path therefore fails
authentication — preventing silent file-swap attacks.

### API

```rust
use wf::core::crypto::{encrypt, decrypt, EncryptOptions, DecryptOptions, SivMode};

// Encrypt
let opts = EncryptOptions {
    siv_mode: SivMode::LocalDeterministic { context: "secrets/key.pem".to_owned() },
    ..Default::default()
};
let ciphertext_b64 = encrypt(plaintext, "my-password", &opts)?;

// Decrypt
let dec_opts = DecryptOptions {
    verify_context: Some("secrets/key.pem".to_owned()),
    ..Default::default()
};
let plaintext = decrypt(&ciphertext_b64, "my-password", &dec_opts)?;
```

### `CryptoError` variants

| Variant | When |
|---|---|
| `AuthenticationFailed` | Wrong password, wrong AAD, or tampered ciphertext |
| `IntegrityCheckFailed` | AEAD passed but SIV IV mismatch (content-level tampering) |
| `MissingSaltHeader` | Packet does not start with `"Salted__"` |
| `MissingIVHeader` | Packet missing `"IVed__"` after the salt |
| `DataTooShort` | Packet too short to contain a valid IV + tag |
| `DigestTooShort` | Selected hash produces fewer bytes than SIV requires |
| `Kdf(msg)` | KDF parameter validation error (e.g. invalid Argon2 params) |
| `Base64(err)` | Input is not valid standard base64 |

---

## Dependency graph

```
main.rs
  └── cli.rs              (GlobalOptions, Resolver, Commands)
        ├── core/log.rs   ← no wf deps
        ├── core/process.rs  ← uses log
        ├── core/git.rs      ← uses process, log
        ├── core/config.rs   ← uses log; serde + toml
        └── core/crypto.rs   ← no wf deps; RustCrypto crates

cmd/crypt.rs  ← uses cli, core/crypto, core/git
cmd/builder.rs, cmd/stack.rs, cmd/gpu.rs, cmd/remote.rs  ← stubs
```
