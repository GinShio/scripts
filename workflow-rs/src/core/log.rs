//! Global logging subsystem.
//!
//! Provides a lean, zero-dependency custom [`log::Log`] implementation that
//! surfaces the two global flags driving runtime behaviour across the entire
//! application:
//!
//! - **verbose** (`-v`) — enables `DEBUG`-level messages (filtered out by default).
//! - **dry-run** (`-n`) — suppresses real side effects; commands print a
//!   `[DRY-RUN]` banner to **stdout** instead of executing.
//!
//! # Initialisation
//!
//! Call [`init`] exactly once at program start (typically inside `main`) before
//! any log macros or [`Command`][crate::core::process::Command] calls are made.
//!
//! ```no_run
//! wf::core::log::init(/* verbose */ true, /* dry_run */ false);
//! log::debug!("Debug message, visible because verbose=true");
//! ```
//!
//! # Dry-run output
//!
//! Use the free function [`dry_run`] (or its format counterpart [`dry_run_fmt`])
//! to emit what *would* be done.  Output goes to **stdout** so it can be
//! captured by shell scripts:
//!
//! ```no_run
//! wf::core::log::dry_run("git push origin main");
//! // stdout: [DRY-RUN] git push origin main
//! ```

use std::sync::atomic::{AtomicBool, Ordering};

// ---------------------------------------------------------------------------
// Global state
// ---------------------------------------------------------------------------

static VERBOSE: AtomicBool = AtomicBool::new(false);
static DRY_RUN: AtomicBool = AtomicBool::new(false);

/// Returns `true` if verbose mode is active.
#[inline]
pub fn is_verbose() -> bool {
    VERBOSE.load(Ordering::Relaxed)
}

/// Returns `true` if dry-run mode is active.
#[inline]
pub fn is_dry_run() -> bool {
    DRY_RUN.load(Ordering::Relaxed)
}

// ---------------------------------------------------------------------------
// Initialisation
// ---------------------------------------------------------------------------

/// Initialises the logging subsystem.
///
/// Sets the global verbose and dry-run flags, registers the custom logger, and
/// configures the [`log`] crate's maximum level filter accordingly.
///
/// Calling this function more than once is safe — subsequent calls update the
/// flags but are otherwise no-ops (the logger is only registered once).
pub fn init(verbose: bool, dry_run: bool) {
    VERBOSE.store(verbose, Ordering::Relaxed);
    DRY_RUN.store(dry_run, Ordering::Relaxed);

    // Ignore error: means the logger was already installed (e.g. in tests).
    let _ = log::set_logger(&WF_LOGGER);
    log::set_max_level(if verbose {
        log::LevelFilter::Debug
    } else {
        log::LevelFilter::Info
    });
}

// ---------------------------------------------------------------------------
// Logger implementation
// ---------------------------------------------------------------------------

struct WfLogger;

static WF_LOGGER: WfLogger = WfLogger;

impl log::Log for WfLogger {
    fn enabled(&self, metadata: &log::Metadata<'_>) -> bool {
        if metadata.level() == log::Level::Debug {
            VERBOSE.load(Ordering::Relaxed)
        } else {
            metadata.level() <= log::Level::Warn
        }
    }

    fn log(&self, record: &log::Record<'_>) {
        if !self.enabled(record.metadata()) {
            return;
        }

        // Produce: "[LEVEL] (scope) message" mirroring the Zig format.
        let level = record.level().as_str().to_uppercase();
        let target = record.target();

        // Trim the crate path prefix so the scope is short and readable.
        let scope = target
            .rsplit("::")
            .next()
            .filter(|s| *s != "wf")
            .unwrap_or("");

        if scope.is_empty() {
            eprintln!("[{level}] {}", record.args());
        } else {
            eprintln!("[{level}] ({scope}) {}", record.args());
        }
    }

    fn flush(&self) {}
}

// ---------------------------------------------------------------------------
// Dry-run helpers
// ---------------------------------------------------------------------------

/// Emits a `[DRY-RUN] <msg>` line to **stdout** when dry-run mode is active.
///
/// This is a no-op when dry-run mode is disabled, making it safe to call
/// unconditionally inside command implementations.
pub fn dry_run(msg: &str) {
    if is_dry_run() {
        println!("[DRY-RUN] {msg}");
    }
}

/// Format-string variant of [`dry_run`].
///
/// # Example
/// ```no_run
/// wf::core::log::dry_run_fmt(format_args!("git commit -m {:?}", "Initial"));
/// ```
pub fn dry_run_fmt(args: std::fmt::Arguments<'_>) {
    if is_dry_run() {
        println!("[DRY-RUN] {args}");
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn init_sets_flags() {
        init(true, true);
        assert!(is_verbose());
        assert!(is_dry_run());

        init(false, false);
        assert!(!is_verbose());
        assert!(!is_dry_run());
    }
}
