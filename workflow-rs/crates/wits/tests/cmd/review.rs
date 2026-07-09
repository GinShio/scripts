//! Black-box tests for `wits review`'s local pipeline.
//!
//! The network verbs (`fetch`, `submit`) talk to a live forge, which a unit test
//! can't stand up; but everything *between* them is local and is where the
//! elegance lives — the store, the draft, the merged view, the `--json`
//! contract. So these drive the real binary against a throwaway git repo with a
//! hand-seeded store (simulating a completed `fetch`) and pin the authoring
//! loop: seed → show → comment → verdict → draft → show → drop, plus a
//! `submit --dry-run` that plans without touching the network.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

/// A throwaway repo (with an `origin` remote so the forge identity resolves) and
/// an isolated review store, reused across several commands.
struct Fixture {
    _dir: tempfile::TempDir,
    repo: PathBuf,
    store: PathBuf,
}

struct Out {
    success: bool,
    stdout: String,
    stderr: String,
}

impl Fixture {
    fn new() -> Fixture {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        let store = dir.path().join("store");
        std::fs::create_dir_all(&repo).unwrap();
        std::fs::create_dir_all(&store).unwrap();

        let git = |args: &[&str]| {
            let ok = Command::new("git")
                .args(args)
                .current_dir(&repo)
                .env("GIT_CONFIG_GLOBAL", "/dev/null")
                .env("GIT_CONFIG_SYSTEM", "/dev/null")
                .status()
                .unwrap()
                .success();
            assert!(ok, "git {args:?} failed");
        };
        git(&["init", "-b", "main"]);
        git(&["remote", "add", "origin", "git@github.com:me/proj.git"]);

        Fixture {
            _dir: dir,
            repo,
            store,
        }
    }

    /// The store path for MR `id`'s cache under this fixture's identity.
    fn cache_path(&self, id: &str) -> PathBuf {
        self.store
            .join("github.com/me/proj/remote")
            .join(format!("mr-{id}.json"))
    }

    /// Seed a completed fetch: write a remote cache with one remote thread.
    fn seed(&self, id: &str, head_sha: &str) {
        let template = r##"{
          "schema": 1,
          "mr": {
            "id": "__ID__", "display": "#__ID__", "state": "open", "draft": false,
            "title": "Add a thing", "author": "alice",
            "base": "main", "source": "feature-__ID__", "head_sha": "__HEAD__",
            "updated_at": "2026-07-01T00:00:00Z", "labels": [],
            "web_url": "https://github.com/me/proj/pull/__ID__"
          },
          "version": { "base_sha": "base000", "start_sha": "base000", "head_sha": "__HEAD__" },
          "fetched_at": "1700000000",
          "commits": [{ "sha": "__HEAD__", "subject": "Add a thing" }],
          "files": [{ "path": "src/x.c", "status": "M" }],
          "threads": [{
            "id": "remote:100", "origin": "remote", "resolved": false, "outdated": false,
            "placement": { "kind": "line", "path": "src/x.c", "side": "new", "line": 5 },
            "comments": [{
              "id": "remote:100", "author": "bob", "origin": "remote",
              "body": "nit here", "created_at": "2026-07-01T00:00:00Z", "state": "published"
            }]
          }]
        }"##;
        let cache = template.replace("__ID__", id).replace("__HEAD__", head_sha);
        let path = self.cache_path(id);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, cache).unwrap();
    }

    fn run(&self, args: &[&str], stdin: &[u8]) -> Out {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_wits"));
        cmd.args(args)
            .current_dir(&self.repo)
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_CONFIG_SYSTEM", "/dev/null")
            .env("WITS_REVIEW_DIR", &self.store)
            // A token so the forge resolves for a dry-run submit; no network is
            // reached because the mutation is only previewed.
            .env("GITHUB_TOKEN", "x")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = cmd.spawn().unwrap();
        child.stdin.take().unwrap().write_all(stdin).unwrap();
        let output = child.wait_with_output().unwrap();
        Out {
            success: output.status.success(),
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        }
    }

    fn draft_exists(&self, id: &str) -> bool {
        Path::new(&self.store)
            .join("github.com/me/proj/draft")
            .join(format!("mr-{id}.json"))
            .exists()
    }
}

#[test]
fn show_reflects_the_seeded_remote_thread() {
    let fx = Fixture::new();
    fx.seed("1", "head111");

    let out = fx.run(&["review", "show", "1", "--json"], b"");
    assert!(out.success, "stderr: {}", out.stderr);
    assert!(out.stdout.contains("\"head_sha\": \"head111\""));
    assert!(out.stdout.contains("\"remote:100\""));
    assert!(out.stdout.contains("nit here"));
}

#[test]
fn inbox_lists_fetched_mrs() {
    let fx = Fixture::new();
    fx.seed("1", "head111");
    fx.seed("2", "head222");

    let out = fx.run(&["review", "show", "--json"], b"");
    assert!(out.success, "stderr: {}", out.stderr);
    assert!(out.stdout.contains("\"id\": \"1\""));
    assert!(out.stdout.contains("\"id\": \"2\""));
}

#[test]
fn authoring_loop_builds_and_edits_a_draft() {
    let fx = Fixture::new();
    fx.seed("1", "head111");

    // A line comment (body on stdin) and a reply to the remote thread.
    let c = fx.run(
        &["review", "comment", "1", "--line", "src/x.c:10"],
        b"looks off",
    );
    assert!(c.success, "stderr: {}", c.stderr);
    let r = fx.run(
        &["review", "comment", "1", "--reply", "remote:100"],
        b"agreed",
    );
    assert!(r.success, "stderr: {}", r.stderr);

    // A verdict.
    let v = fx.run(&["review", "verdict", "1", "approve"], b"");
    assert!(v.success, "stderr: {}", v.stderr);

    // The draft records all three.
    let d = fx.run(&["review", "draft", "1", "--json"], b"");
    assert!(d.success, "stderr: {}", d.stderr);
    assert!(d.stdout.contains("\"approve\""));
    assert!(d.stdout.contains("\"local:1\""));
    assert!(d.stdout.contains("\"local:2\""));
    assert!(d.stdout.contains("looks off"));

    // `show` folds the pending draft into the thread view.
    let s = fx.run(&["review", "show", "1", "--json"], b"");
    assert!(s.stdout.contains("\"pending\""));
    assert!(
        s.stdout.contains("agreed"),
        "reply should attach to remote thread"
    );

    // Drop the line comment; the reply and verdict remain.
    let drop = fx.run(&["review", "drop", "1", "local:1"], b"");
    assert!(drop.success, "stderr: {}", drop.stderr);
    let d2 = fx.run(&["review", "draft", "1", "--json"], b"");
    assert!(!d2.stdout.contains("looks off"));
    assert!(d2.stdout.contains("\"local:2\""));
}

#[test]
fn dropping_the_last_action_removes_the_draft_file() {
    let fx = Fixture::new();
    fx.seed("1", "head111");
    fx.run(&["review", "comment", "1", "--mr-level"], b"just a note");
    assert!(fx.draft_exists("1"));
    fx.run(&["review", "drop", "1", "local:1"], b"");
    // A verdict is still absent, so the draft is now empty and its file is gone.
    assert!(!fx.draft_exists("1"), "empty draft file should be removed");
}

#[test]
fn submit_dry_run_plans_without_touching_the_network() {
    let fx = Fixture::new();
    fx.seed("1", "head111");
    fx.run(
        &["review", "comment", "1", "--line", "src/x.c:10"],
        b"please fix",
    );
    fx.run(&["review", "verdict", "1", "request-changes"], b"");

    let out = fx.run(&["review", "submit", "1", "-n"], b"");
    assert!(out.success, "stderr: {}", out.stderr);
    // The plan lands on stdout (the scriptable dry-run stream).
    assert!(out.stdout.contains("[DRY-RUN]"), "stdout: {}", out.stdout);
    assert!(out.stdout.contains("request-changes"));
    assert!(out.stdout.contains("src/x.c:10"));
    // The draft is untouched by a dry run.
    assert!(fx.draft_exists("1"));
}

#[test]
fn unknown_mr_is_a_clean_error_not_a_panic() {
    let fx = Fixture::new();
    let out = fx.run(&["review", "show", "99", "--json"], b"");
    assert!(!out.success);
    assert!(out.stderr.contains("isn't in the store") || out.stderr.contains("fetch"));
}
