# Transparent File Encryption (wf crypt)

The `wf crypt` command is a transparent file encryption tool deeply integrated with Git filters. It provides a zero-dependency, high-performance Zig implementation of the popular `transcrypt` Python script, designed to transparently encrypt sensitive files before they are committed, and decrypt them when checked out.

## Design Philosophy

- **Secure by Default:** Uses modern AEAD ciphers (`aes-256-gcm` or `chacha20-poly1305`) and strong Key Derivation Functions (`pbkdf2` or `argon2id`).
- **Context Isolation:** Supports multiple isolated configurations (contexts) within the same repository.
- **Git Filter Integration:** Once installed, files matching your `.gitattributes` are seamlessly encrypted/decrypted during `git add` and `git checkout`.
- **Zero Configuration Fallback:** Designed to fallback safely to raw bytes if the password isn't available, preventing Git workflow interruptions for team members without access.

---

## Configuration Resolution

Configuration variables are resolved in a strict priority chain to prevent bootstrap loops and ensure explicit overrides win:

1. **CLI Arguments** (Highest Priority)
2. **Environment Variables with Context** (`TRANSCRYPT_<CONTEXT>_<KEY>`)
3. **Environment Variables (Default)** (`TRANSCRYPT_<KEY>`)
4. **Git Config with Context** (`transcrypt.<context>.<key>`)
5. **Git Config (Default)** (`transcrypt.<key>`)
6. **System Defaults** (Lowest Priority)

### System Defaults

- **Cipher**: `aes-256-gcm`
- **KDF**: `pbkdf2`
- **Digest**: `sha256`
- **Iterations**: 
  - `99989` for `pbkdf2` (backward compatible)
  - `4` for `argon2id`

---

## Commands

### 1. Check Status

```bash
wf crypt -c <context> status
```
Displays the current configuration, resolution source for each key, and whether the Git filters are installed.

### 2. Manual Configuration (via Dotfiles)

Since we use dotfiles and standard Git config management, you configure `wf crypt` by directly modifying your `.gitattributes` and `.git/config` (or global `~/.gitconfig`):

**1. Register the Git Filters**
Add the following to your `git config`:
```ini
[filter "transcrypt"]
    clean = wf crypt clean %f
    smudge = wf crypt smudge %f
    required = true
[diff "transcrypt"]
    textconv = wf crypt textconv
```
*(If using a custom context like `-c prod`, the filter name will be `transcrypt-prod`)*

**2. Enforce Encryption in .gitattributes:**
```gitattributes
sensitive_file.txt filter=transcrypt diff=transcrypt merge=transcrypt
secret/**/*.json filter=transcrypt diff=transcrypt merge=transcrypt
```

### 3. Internal Commands (Git Hooks)

These commands are automatically executed by Git. **Do not run them manually** unless debugging.

- `wf crypt clean [file]`: Reads plaintext from `stdin`, outputs encrypted base64 to `stdout`.
- `wf crypt smudge [file]`: Reads encrypted base64 from `stdin`, outputs plaintext to `stdout`.
- `wf crypt textconv <file>`: Reads file, outputs plaintext for `git diff`.

---

## Fallback Mode

If a user checks out an encrypted repository but hasn't configured their password, the `smudge` filter degrades gracefully and checks out the raw base64 encrypted data.

If a user has configured a wrong password, the process will fail loudly. If you want to bypass the error and force Git to checkout the encrypted file even on a failure, set:

```bash
export TRANSCRYPT_ALLOW_RAW_FALLBACK=1
```
