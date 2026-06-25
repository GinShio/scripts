//! Subprocess orchestration with dry-run and verbose-logging support.
//!
//! [`Command`] is a fluent builder for shell commands.  It wraps
//! [`std::process::Command`] and adds two cross-cutting concerns that are
//! pervasive in workflow tooling:
//!
//! - **Dry-run** — when the global dry-run flag is set, commands are *printed*
//!   instead of executed.  Read-only commands (e.g. `git config --get`) can
//!   opt out via [`Command::force_run`] so control-flow always works.
//! - **Verbose logging** — every execution emits a `[DEBUG]` line showing the
//!   full argument list and working directory.
//!
//! # Usage
//!
//! ```no_run
//! use wf::core::process::Command;
//!
//! // Capture output
//! let result = Command::new("git")
//!     .arg("rev-parse")
//!     .arg("HEAD")
//!     .force_run()   // bypass dry-run; this is a read-only query
//!     .exec()?;
//!
//! println!("HEAD: {}", result.stdout_trimmed());
//!
//! // Stream output directly to the terminal (build systems, tests, etc.)
//! Command::new("cargo")
//!     .args(["build", "--release"])
//!     .stream_check()?;
//! # Ok::<(), wf::core::process::ProcessError>(())
//! ```

use std::path::PathBuf;
use std::process::Stdio;
use thiserror::Error;

use crate::core::log as wf_log;

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

/// Errors that can arise during command execution.
#[derive(Debug, Error)]
pub enum ProcessError {
    /// The command was spawned successfully but exited with a non-zero status.
    #[error("command failed (exit {exit_code}): {cmd}\nstderr: {stderr}")]
    Failed {
        cmd: String,
        exit_code: i32,
        stdout: String,
        stderr: String,
    },
    /// The OS rejected the `spawn` call (e.g. executable not found).
    #[error("failed to spawn '{program}': {source}")]
    Spawn {
        program: String,
        #[source]
        source: std::io::Error,
    },
    /// Any other I/O error (pipe broken, etc.).
    #[error("I/O error while running command: {0}")]
    Io(#[from] std::io::Error),
    /// stdout or stderr contained non-UTF-8 bytes.
    #[error("command output is not valid UTF-8: {0}")]
    Utf8(#[from] std::string::FromUtf8Error),
}

// ---------------------------------------------------------------------------
// CommandResult
// ---------------------------------------------------------------------------

/// The outcome of a successfully *spawned* command.
///
/// Note: "successfully spawned" means the process was started and waited for —
/// it does **not** imply the exit code is 0.  Use [`is_success`] to check, or
/// prefer [`Command::exec_check`] / [`Command::stream_check`] which return
/// [`ProcessError::Failed`] automatically.
#[derive(Debug)]
pub struct CommandResult {
    /// Raw exit code (`0` for dry-run results).
    pub exit_code: i32,
    /// Captured standard output (empty for streamed commands).
    pub stdout: String,
    /// Captured standard error (empty for streamed commands).
    pub stderr: String,
    /// `true` when output was inherited by the terminal rather than captured.
    pub streamed: bool,
}

impl CommandResult {
    /// Returns `true` if the exit code is 0.
    #[inline]
    pub fn is_success(&self) -> bool {
        self.exit_code == 0
    }

    /// Returns `stdout` with trailing `\r\n` stripped — useful for single-line
    /// git outputs like commit hashes or branch names.
    #[inline]
    pub fn stdout_trimmed(&self) -> &str {
        self.stdout.trim_end_matches(['\r', '\n'])
    }
}

// ---------------------------------------------------------------------------
// Command builder
// ---------------------------------------------------------------------------

/// A fluent, chainable command builder.
///
/// Methods that configure the command take `&mut self` and return `&mut Self`
/// so you can chain calls on a `let mut cmd = Command::new(...)` binding.
/// Call [`exec`][Command::exec] or [`stream`][Command::stream] to run.
pub struct Command {
    program: String,
    argv: Vec<String>,
    cwd: Option<PathBuf>,
    env_overrides: Vec<(String, String)>,
    /// When `true`, skip the dry-run guard and always execute.
    force_run: bool,
}

impl Command {
    /// Creates a new builder for `program` (the executable path or name).
    pub fn new(program: impl Into<String>) -> Self {
        let program = program.into();
        Self {
            argv: vec![program.clone()],
            program,
            cwd: None,
            env_overrides: Vec::new(),
            force_run: false,
        }
    }

    /// Appends a single argument.
    pub fn arg(&mut self, a: impl Into<String>) -> &mut Self {
        self.argv.push(a.into());
        self
    }

    /// Appends multiple arguments.
    pub fn args<I, S>(&mut self, iter: I) -> &mut Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.argv.extend(iter.into_iter().map(Into::into));
        self
    }

    /// Sets the working directory for the command.
    pub fn current_dir(&mut self, path: impl Into<PathBuf>) -> &mut Self {
        self.cwd = Some(path.into());
        self
    }

    /// Adds a single environment variable override.
    pub fn env(&mut self, key: impl Into<String>, val: impl Into<String>) -> &mut Self {
        self.env_overrides.push((key.into(), val.into()));
        self
    }

    /// Forces the command to run even when the global dry-run flag is set.
    ///
    /// Use this for **read-only** queries (e.g. `git config --get`, `git status`)
    /// that are required for control-flow decisions even during a dry-run.
    pub fn force_run(&mut self) -> &mut Self {
        self.force_run = true;
        self
    }

    // -----------------------------------------------------------------------
    // Internal helpers
    // -----------------------------------------------------------------------

    fn format_cmd(&self) -> String {
        let parts: Vec<String> = self
            .argv
            .iter()
            .map(|a| {
                if a.contains(' ') {
                    format!("\"{a}\"")
                } else {
                    a.clone()
                }
            })
            .collect();
        let mut s = parts.join(" ");
        if let Some(cwd) = &self.cwd {
            s.push_str(&format!(" (cwd={})", cwd.display()));
        }
        s
    }

    fn build_std_command(&self) -> std::process::Command {
        let mut cmd = std::process::Command::new(&self.program);
        // argv[0] is the program; remaining entries are arguments.
        cmd.args(&self.argv[1..]);
        if let Some(cwd) = &self.cwd {
            cmd.current_dir(cwd);
        }
        for (k, v) in &self.env_overrides {
            cmd.env(k, v);
        }
        cmd
    }

    // -----------------------------------------------------------------------
    // Execution
    // -----------------------------------------------------------------------

    /// Executes the command, capturing stdout and stderr.
    ///
    /// Returns a [`CommandResult`] regardless of exit code.  If the global
    /// dry-run flag is set (and [`force_run`][Command::force_run] was not
    /// called), prints the command and returns a synthetic success result.
    pub fn exec(&self) -> Result<CommandResult, ProcessError> {
        if wf_log::is_dry_run() && !self.force_run {
            let s = self.format_cmd();
            wf_log::dry_run(&s);
            return Ok(CommandResult {
                exit_code: 0,
                stdout: String::new(),
                stderr: String::new(),
                streamed: false,
            });
        }

        if wf_log::is_verbose() {
            log::debug!("Executing: {}", self.format_cmd());
        }

        let output = self
            .build_std_command()
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .output()
            .map_err(|e| ProcessError::Spawn {
                program: self.program.clone(),
                source: e,
            })?;

        Ok(CommandResult {
            exit_code: output.status.code().unwrap_or(-1),
            stdout: String::from_utf8(output.stdout)?,
            stderr: String::from_utf8(output.stderr)?,
            streamed: false,
        })
    }

    /// Executes the command and returns [`ProcessError::Failed`] if the exit
    /// code is non-zero, logging stderr automatically.
    pub fn exec_check(&self) -> Result<CommandResult, ProcessError> {
        let result = self.exec()?;
        if !result.is_success() {
            log::error!(
                "Command failed: {}",
                if !result.stderr.is_empty() {
                    &result.stderr
                } else {
                    &result.stdout
                }
            );
            return Err(ProcessError::Failed {
                cmd: self.format_cmd(),
                exit_code: result.exit_code,
                stdout: result.stdout,
                stderr: result.stderr,
            });
        }
        Ok(result)
    }

    /// Executes the command with stdout/stderr **inherited** by the current
    /// process (i.e. directly streamed to the terminal).
    ///
    /// Useful for long-running commands like builds or test runners where
    /// real-time progress output is important.  Returns a [`CommandResult`]
    /// with empty `stdout`/`stderr` fields and `streamed = true`.
    pub fn stream(&self) -> Result<CommandResult, ProcessError> {
        if wf_log::is_dry_run() && !self.force_run {
            let s = format!("{} [streamed]", self.format_cmd());
            wf_log::dry_run(&s);
            return Ok(CommandResult {
                exit_code: 0,
                stdout: String::new(),
                stderr: String::new(),
                streamed: true,
            });
        }

        if wf_log::is_verbose() {
            log::debug!("Streaming: {}", self.format_cmd());
        }

        let status = self
            .build_std_command()
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .status()
            .map_err(|e| ProcessError::Spawn {
                program: self.program.clone(),
                source: e,
            })?;

        Ok(CommandResult {
            exit_code: status.code().unwrap_or(-1),
            stdout: String::new(),
            stderr: String::new(),
            streamed: true,
        })
    }

    /// Streams the command and returns [`ProcessError::Failed`] if the exit
    /// code is non-zero.
    pub fn stream_check(&self) -> Result<CommandResult, ProcessError> {
        let result = self.stream()?;
        if !result.is_success() {
            log::error!("Streamed command failed: {}", self.format_cmd());
            return Err(ProcessError::Failed {
                cmd: self.format_cmd(),
                exit_code: result.exit_code,
                stdout: String::new(),
                stderr: String::new(),
            });
        }
        Ok(result)
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exec_captures_stdout() {
        let result = Command::new("echo").arg("hello").exec().unwrap();
        assert!(result.is_success());
        assert_eq!(result.stdout_trimmed(), "hello");
    }

    #[test]
    fn exec_check_fails_on_nonzero() {
        // Use force_run() to bypass any dry-run state left over from a
        // concurrently-running test that may have set the global flag.
        let err = Command::new("false").force_run().exec_check().unwrap_err();
        assert!(matches!(err, ProcessError::Failed { .. }));
    }

    #[test]
    fn dry_run_returns_success_without_executing() {
        crate::core::log::init(false, true);
        // 'false' would normally fail, but dry-run intercepts it.
        let result = Command::new("false").exec_check().unwrap();
        assert!(result.is_success());
        crate::core::log::init(false, false);
    }

    #[test]
    fn force_run_bypasses_dry_run() {
        crate::core::log::init(false, true);
        // With force_run, the command actually executes.
        let result = Command::new("echo")
            .arg("forced")
            .force_run()
            .exec()
            .unwrap();
        assert_eq!(result.stdout_trimmed(), "forced");
        crate::core::log::init(false, false);
    }
}
