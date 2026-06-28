//! Black-box tests for the `wf transcrypt` git-filter entry points.
//!
//! These spawn the real binary and drive it the way git does — bytes in on
//! stdin, bytes out on stdout — because what matters here is the contract with
//! git, not any single internal function. The behaviour worth pinning is the
//! clean/smudge round-trip and the deliberate split between a *missing*
//! password (degrade quietly so a checkout still works) and a *wrong* one (fail
//! loudly rather than write garbage to the working tree).

use std::io::Write;
use std::process::{Command, Stdio};

struct Run {
    success: bool,
    stdout: Vec<u8>,
}

/// Run the binary in a throwaway directory with git's global/system config
/// pointed at nothing, so the only configuration in play is what we set here.
fn run(args: &[&str], env: &[(&str, &str)], unset: &[&str], stdin: &[u8]) -> Run {
    let dir = tempfile::tempdir().unwrap();
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_wf"));
    cmd.args(args)
        .current_dir(dir.path())
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_SYSTEM", "/dev/null")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for key in unset {
        cmd.env_remove(key);
    }
    for (key, value) in env {
        cmd.env(key, value);
    }

    let mut child = cmd.spawn().unwrap();
    child.stdin.take().unwrap().write_all(stdin).unwrap();
    let output = child.wait_with_output().unwrap();
    Run {
        success: output.status.success(),
        stdout: output.stdout,
    }
}

const PASSWORD: (&str, &str) = ("TRANSCRYPT_PASSWORD", "test-passphrase");

#[test]
fn clean_then_smudge_round_trips() {
    let plaintext: &[u8] = b"top secret value\n";

    let cleaned = run(
        &["transcrypt", "clean", "secret.txt"],
        &[PASSWORD],
        &[],
        plaintext,
    );
    assert!(cleaned.success);
    assert_ne!(
        cleaned.stdout.as_slice(),
        plaintext,
        "clean should have encrypted"
    );

    let smudged = run(
        &["transcrypt", "smudge", "secret.txt"],
        &[PASSWORD],
        &[],
        &cleaned.stdout,
    );
    assert!(smudged.success);
    assert_eq!(smudged.stdout.as_slice(), plaintext);
}

#[test]
fn smudge_without_a_password_passes_bytes_through() {
    let blob: &[u8] = b"not-really-ciphertext but still checked out\n";
    let out = run(
        &["transcrypt", "smudge", "secret.txt"],
        &[],
        &["TRANSCRYPT_PASSWORD"],
        blob,
    );
    assert!(out.success);
    assert_eq!(out.stdout.as_slice(), blob);
}

#[test]
fn smudge_with_the_wrong_password_fails_loudly() {
    let cleaned = run(
        &["transcrypt", "clean", "secret.txt"],
        &[PASSWORD],
        &[],
        b"data\n",
    );
    let out = run(
        &["transcrypt", "smudge", "secret.txt"],
        &[("TRANSCRYPT_PASSWORD", "the-wrong-one")],
        &[],
        &cleaned.stdout,
    );
    assert!(!out.success);
}

#[test]
fn the_escape_hatch_forces_a_wrong_password_through() {
    let cleaned = run(
        &["transcrypt", "clean", "secret.txt"],
        &[PASSWORD],
        &[],
        b"data\n",
    );
    let out = run(
        &["transcrypt", "smudge", "secret.txt"],
        &[
            ("TRANSCRYPT_PASSWORD", "the-wrong-one"),
            ("TRANSCRYPT_ALLOW_RAW_FALLBACK", "1"),
        ],
        &[],
        &cleaned.stdout,
    );
    assert!(out.success);
    assert_eq!(
        out.stdout, cleaned.stdout,
        "raw ciphertext should pass through unchanged"
    );
}
