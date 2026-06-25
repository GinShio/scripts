//! `wf crypt` — transparent file encryption for Git.
//!
//! Formerly the Python `transcrypt` script.  Integrates with Git's
//! `filter.*` and `diff.*` mechanisms so that sensitive files are
//! automatically encrypted on `git add` and decrypted on `git checkout`.
//!
//! For the detailed design including the packet format, SIV construction, and
//! configuration resolution chain, see [`docs/crypt.md`](../../docs/crypt.md).
//!
//! # Git filter integration (manual setup)
//!
//! Add to `.git/config` (or `~/.gitconfig`):
//!
//! ```ini
//! [filter "transcrypt"]
//!     clean   = wf crypt clean %f
//!     smudge  = wf crypt smudge %f
//!     required = true
//! [diff "transcrypt"]
//!     textconv = wf crypt textconv
//! ```
//!
//! Add to `.gitattributes`:
//!
//! ```gitattributes
//! secrets/**  filter=transcrypt diff=transcrypt merge=transcrypt
//! ```
//!
//! # Subcommands
//!
//! | Subcommand | Description |
//! |---|---|
//! | `status` | Show current configuration and filter installation status |
//! | `clean` | Git clean filter: encrypt plaintext from stdin → base64 on stdout |
//! | `smudge` | Git smudge filter: decrypt base64 from stdin → plaintext on stdout |
//! | `textconv` | Git textconv: decrypt a file for `git diff` display |

use std::io::{Read, Write};

use clap::Args;

use crate::cli::{GlobalOptions, Resolver};
use crate::core::crypto::{
    CipherAlgorithm, DecryptOptions, EncryptOptions, HashAlgorithm, KdfAlgorithm, SivMode,
};
use crate::core::git::Repository;

// ---------------------------------------------------------------------------
// CLI args
// ---------------------------------------------------------------------------

/// Arguments for `wf crypt`.
#[derive(Debug, Args)]
pub struct CryptArgs {
    /// Context name for multi-context repositories (default: `"default"`).
    #[arg(short = 'C', long, value_name = "CONTEXT", default_value = "default")]
    pub context: String,

    #[command(subcommand)]
    pub action: CryptAction,
}

/// Subcommands for `wf crypt`.
#[derive(Debug, clap::Subcommand)]
pub enum CryptAction {
    /// Show the current configuration and filter installation status.
    Status,
    /// Git clean filter: read plaintext from stdin, write base64 ciphertext to stdout.
    Clean {
        /// File path (passed by Git as `%f`), used as AEAD context.
        #[arg(value_name = "FILE")]
        file: Option<String>,
    },
    /// Git smudge filter: read base64 ciphertext from stdin, write plaintext to stdout.
    Smudge {
        /// File path (passed by Git as `%f`), used as AEAD context.
        #[arg(value_name = "FILE")]
        file: Option<String>,
    },
    /// Git textconv: decrypt a file for `git diff` display.
    Textconv {
        /// Path to the encrypted file on disk.
        #[arg(value_name = "FILE")]
        file: String,
    },
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

/// Entry point for `wf crypt`.
pub fn run(global: &GlobalOptions, args: &CryptArgs) -> anyhow::Result<()> {
    let cwd = std::env::current_dir()?;
    let repo = Repository::new(&cwd);
    let mut resolver = Resolver::new(Some(&repo), "transcrypt", Some(args.context.as_str()));

    // Propagate any CLI-level options that the resolver should know about.
    // (In a full implementation, --password, --cipher, etc. would be injected here.)

    match &args.action {
        CryptAction::Status => status(global, &resolver, &args.context),
        CryptAction::Clean { file } => clean(global, &mut resolver, file.as_deref()),
        CryptAction::Smudge { file } => smudge(global, &mut resolver, file.as_deref()),
        CryptAction::Textconv { file } => textconv(global, &mut resolver, file),
    }
}

// ---------------------------------------------------------------------------
// Subcommand implementations
// ---------------------------------------------------------------------------

fn status(_global: &GlobalOptions, resolver: &Resolver<'_>, context: &str) -> anyhow::Result<()> {
    println!("wf crypt status (context: {context})");
    println!();

    for key in ["password", "cipher", "digest", "kdf", "iterations"] {
        match resolver.get(key) {
            Some(rv) => {
                let display = if key == "password" { "***" } else { &rv.value };
                println!("  {key:<12} = {display:<20}  [{}]", rv.source);
            }
            None => println!("  {key:<12} = (not set)"),
        }
    }

    Ok(())
}

fn resolve_crypto_config(
    resolver: &Resolver<'_>,
) -> anyhow::Result<(String, CipherAlgorithm, HashAlgorithm, KdfAlgorithm, u32)> {
    let password = resolver
        .get("password")
        .map(|rv| rv.value)
        .ok_or_else(|| anyhow::anyhow!("No password configured. Set TRANSCRYPT_PASSWORD or run 'git config transcrypt.password <pwd>'"))?;

    let cipher: CipherAlgorithm = resolver
        .get_or_default("cipher", "aes-256-gcm")
        .value
        .parse()
        .unwrap_or(CipherAlgorithm::Aes256Gcm);

    let hash: HashAlgorithm = resolver
        .get_or_default("digest", "sha256")
        .value
        .parse()
        .unwrap_or(HashAlgorithm::Sha256);

    let kdf: KdfAlgorithm = resolver
        .get_or_default("kdf", "pbkdf2")
        .value
        .parse()
        .unwrap_or(KdfAlgorithm::Pbkdf2);

    let iters: u32 = resolver
        .get_or_default("iterations", "")
        .value
        .parse()
        .unwrap_or_else(|_| kdf.default_iterations());

    Ok((password, cipher, hash, kdf, iters))
}

fn clean(
    _global: &GlobalOptions,
    resolver: &mut Resolver<'_>,
    file: Option<&str>,
) -> anyhow::Result<()> {
    let (password, cipher, hash, kdf, iters) = resolve_crypto_config(resolver)?;
    let context = file.unwrap_or("").to_owned();

    let mut plaintext = Vec::new();
    std::io::stdin().read_to_end(&mut plaintext)?;

    let opts = EncryptOptions {
        cipher,
        kdf,
        hash,
        iterations: Some(iters),
        siv_mode: SivMode::LocalDeterministic { context },
    };

    let ciphertext = crate::core::crypto::encrypt(&plaintext, &password, &opts)?;
    std::io::stdout().write_all(ciphertext.as_bytes())?;
    std::io::stdout().write_all(b"\n")?;
    Ok(())
}

fn smudge(
    _global: &GlobalOptions,
    resolver: &mut Resolver<'_>,
    file: Option<&str>,
) -> anyhow::Result<()> {
    let context = file.unwrap_or("").to_owned();

    // If no password is configured, pass the encrypted content through
    // unchanged (graceful degradation — the user simply sees the raw
    // base64 text rather than an abort).
    let config = resolve_crypto_config(resolver);
    if config.is_err() {
        let mut buf = Vec::new();
        std::io::stdin().read_to_end(&mut buf)?;
        std::io::stdout().write_all(&buf)?;
        return Ok(());
    }
    let (password, cipher, hash, kdf, iters) = config.unwrap();

    let mut ciphertext_b64 = String::new();
    std::io::stdin().read_to_string(&mut ciphertext_b64)?;

    let opts = DecryptOptions {
        cipher,
        kdf,
        hash,
        iterations: Some(iters),
        verify_context: Some(context),
    };

    match crate::core::crypto::decrypt(ciphertext_b64.trim(), &password, &opts) {
        Ok(plaintext) => {
            std::io::stdout().write_all(&plaintext)?;
        }
        Err(e) => {
            // If TRANSCRYPT_ALLOW_RAW_FALLBACK is set, fall back to raw output.
            if std::env::var("TRANSCRYPT_ALLOW_RAW_FALLBACK").is_ok() {
                log::warn!("Decryption failed ({e}), falling back to raw content");
                std::io::stdout().write_all(ciphertext_b64.as_bytes())?;
            } else {
                return Err(e.into());
            }
        }
    }
    Ok(())
}

fn textconv(
    _global: &GlobalOptions,
    resolver: &mut Resolver<'_>,
    file: &str,
) -> anyhow::Result<()> {
    let (password, cipher, hash, kdf, iters) = resolve_crypto_config(resolver)?;

    let ciphertext_b64 = std::fs::read_to_string(file)?;
    let opts = DecryptOptions {
        cipher,
        kdf,
        hash,
        iterations: Some(iters),
        verify_context: Some(file.to_owned()),
    };

    let plaintext = crate::core::crypto::decrypt(ciphertext_b64.trim(), &password, &opts)?;
    std::io::stdout().write_all(&plaintext)?;
    Ok(())
}
