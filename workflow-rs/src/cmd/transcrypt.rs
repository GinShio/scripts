//! `wf transcrypt` — transparent file encryption, wired into git's filter system.
//!
//! Git lets a repository declare, per path, a *clean* filter (run on the way
//! into the index) and a *smudge* filter (run on the way out to the working
//! tree). Pointing both at this command turns committing a secret into storing
//! ciphertext and checking it out into recovering plaintext, with nothing in
//! the normal `git add` / `git checkout` workflow to remember. `textconv` is
//! the same idea for `git diff`, so diffs read as plaintext.
//!
//! The one design decision worth understanding is what happens to a teammate
//! who clones the repo without the password. The naive thing — error out — would
//! break their checkout entirely. Instead `smudge` degrades to passing the
//! encrypted bytes through untouched: they get an unreadable-but-harmless blob
//! and a working `git`, rather than a wedged repository. A *wrong* password is
//! a different story and fails loudly, because quietly writing garbage to disk
//! would be worse than stopping.
//!
//! These subcommands are meant to be invoked by git, not by hand. Configure
//! them in `.git/config` and `.gitattributes`; see `docs/transcrypt.md`.

use std::io::{Read, Write};

use clap::Args;

use crate::core::crypto::{
    CipherAlgorithm, DecryptOptions, EncryptOptions, HashAlgorithm, KdfAlgorithm, SivMode,
};
use crate::core::git::Repository;
use crate::core::resolver::Resolver;

#[derive(Debug, Args)]
pub struct TranscryptArgs {
    /// Selects one of several independent secret sets in the same repository.
    #[arg(short = 'C', long, value_name = "CONTEXT", default_value = "default")]
    pub context: String,

    #[command(subcommand)]
    pub action: TranscryptAction,
}

#[derive(Debug, clap::Subcommand)]
pub enum TranscryptAction {
    /// Report the resolved configuration and where each value came from.
    Status,
    /// Clean filter: plaintext on stdin, base64 ciphertext on stdout.
    Clean {
        #[arg(value_name = "FILE")]
        file: Option<String>,
    },
    /// Smudge filter: base64 ciphertext on stdin, plaintext on stdout.
    Smudge {
        #[arg(value_name = "FILE")]
        file: Option<String>,
    },
    /// textconv: decrypt a file on disk to stdout for `git diff`.
    Textconv {
        #[arg(value_name = "FILE")]
        file: String,
    },
}

pub fn run(args: &TranscryptArgs) -> anyhow::Result<()> {
    let cwd = std::env::current_dir()?;
    let repo = Repository::new(&cwd);
    let resolver = Resolver::new(Some(&repo), "transcrypt", Some(args.context.as_str()));

    match &args.action {
        TranscryptAction::Status => status(&resolver, &args.context),
        TranscryptAction::Clean { file } => clean(&resolver, file.as_deref()),
        TranscryptAction::Smudge { file } => smudge(&resolver, file.as_deref()),
        TranscryptAction::Textconv { file } => textconv(&resolver, file),
    }
}

fn status(resolver: &Resolver<'_>, context: &str) -> anyhow::Result<()> {
    println!("wf transcrypt status (context: {context})\n");
    for key in ["password", "cipher", "digest", "kdf", "iterations"] {
        match resolver.get(key) {
            Some(rv) => {
                let shown = if key == "password" { "***" } else { &rv.value };
                println!("  {key:<12} = {shown:<20}  [{}]", rv.source);
            }
            None => println!("  {key:<12} = (not set)"),
        }
    }
    Ok(())
}

/// The encryption settings, gathered from the resolver. The password is the
/// only one that's mandatory; the rest carry the historical defaults so an
/// unconfigured repo still round-trips.
fn resolve_crypto_config(
    resolver: &Resolver<'_>,
) -> anyhow::Result<(String, CipherAlgorithm, HashAlgorithm, KdfAlgorithm, u32)> {
    let password = resolver.get("password").map(|rv| rv.value).ok_or_else(|| {
        anyhow::anyhow!(
            "no password configured; set TRANSCRYPT_PASSWORD or git config transcrypt.password"
        )
    })?;

    let cipher = resolver
        .get_or_default("cipher", "aes-256-gcm")
        .value
        .parse()
        .unwrap_or(CipherAlgorithm::Aes256Gcm);
    let hash = resolver
        .get_or_default("digest", "sha256")
        .value
        .parse()
        .unwrap_or(HashAlgorithm::Sha256);
    let kdf = resolver
        .get_or_default("kdf", "pbkdf2")
        .value
        .parse()
        .unwrap_or(KdfAlgorithm::Pbkdf2);
    let iterations = resolver
        .get_or_default("iterations", "")
        .value
        .parse()
        .unwrap_or_else(|_| kdf.default_iterations());

    Ok((password, cipher, hash, kdf, iterations))
}

fn clean(resolver: &Resolver<'_>, file: Option<&str>) -> anyhow::Result<()> {
    let (password, cipher, hash, kdf, iterations) = resolve_crypto_config(resolver)?;

    let mut plaintext = Vec::new();
    std::io::stdin().read_to_end(&mut plaintext)?;

    let opts = EncryptOptions {
        cipher,
        kdf,
        hash,
        iterations: Some(iterations),
        siv_mode: SivMode::LocalDeterministic {
            context: file.unwrap_or("").to_owned(),
        },
    };
    let ciphertext = crate::core::crypto::encrypt(&plaintext, &password, &opts)?;
    let mut out = std::io::stdout();
    out.write_all(ciphertext.as_bytes())?;
    out.write_all(b"\n")?;
    Ok(())
}

fn smudge(resolver: &Resolver<'_>, file: Option<&str>) -> anyhow::Result<()> {
    // No password isn't an error here — it's the teammate-without-the-key case.
    // Hand back the encrypted bytes verbatim so their checkout still completes.
    let Ok((password, cipher, hash, kdf, iterations)) = resolve_crypto_config(resolver) else {
        let mut passthrough = Vec::new();
        std::io::stdin().read_to_end(&mut passthrough)?;
        std::io::stdout().write_all(&passthrough)?;
        return Ok(());
    };

    let mut ciphertext_b64 = String::new();
    std::io::stdin().read_to_string(&mut ciphertext_b64)?;

    let opts = DecryptOptions {
        cipher,
        kdf,
        hash,
        iterations: Some(iterations),
        verify_context: Some(file.unwrap_or("").to_owned()),
    };

    match crate::core::crypto::decrypt(ciphertext_b64.trim(), &password, &opts) {
        Ok(plaintext) => std::io::stdout().write_all(&plaintext)?,
        Err(e) => {
            // A wrong password means real data would be silently corrupted, so
            // we stop — unless the operator has explicitly opted into writing
            // the raw ciphertext through anyway.
            if std::env::var("TRANSCRYPT_ALLOW_RAW_FALLBACK").is_ok() {
                log::warn!("decryption failed ({e}); writing raw content");
                std::io::stdout().write_all(ciphertext_b64.as_bytes())?;
            } else {
                return Err(e.into());
            }
        }
    }
    Ok(())
}

fn textconv(resolver: &Resolver<'_>, file: &str) -> anyhow::Result<()> {
    let (password, cipher, hash, kdf, iterations) = resolve_crypto_config(resolver)?;

    let ciphertext_b64 = std::fs::read_to_string(file)?;
    let opts = DecryptOptions {
        cipher,
        kdf,
        hash,
        iterations: Some(iterations),
        verify_context: Some(file.to_owned()),
    };
    let plaintext = crate::core::crypto::decrypt(ciphertext_b64.trim(), &password, &opts)?;
    std::io::stdout().write_all(&plaintext)?;
    Ok(())
}
