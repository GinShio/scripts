//! Best-effort host facts for the `system.*` template namespace.
//!
//! `std` gives us CPU parallelism but no cross-platform memory query, so the
//! memory read is per-OS (a `/proc` read on Linux, a `sysctl` on the BSDs and
//! macOS, nothing elsewhere). Kept apart from [`super::context`] on purpose: this
//! is the one spot in the read-only project core that actually probes the OS, so
//! isolating it keeps the context assembly itself pure and testable.

/// Logical CPU count, falling back to 1 when the platform won't say.
pub(crate) fn cpu_count() -> i64 {
    std::thread::available_parallelism()
        .map(|n| n.get() as i64)
        .unwrap_or(1)
}

/// Total physical memory in GiB, or `None` when the platform has no cheap query
/// (which simply resolves `system.memory.total_gb` to 0).
pub(crate) fn total_memory_gb() -> Option<i64> {
    #[cfg(target_os = "linux")]
    {
        let text = std::fs::read_to_string("/proc/meminfo").ok()?;
        let line = text.lines().find(|l| l.starts_with("MemTotal:"))?;
        let kb: i64 = line
            .trim_start_matches("MemTotal:")
            .trim()
            .trim_end_matches("kB")
            .trim()
            .parse()
            .ok()?;
        Some((kb / (1024 * 1024)).max(1))
    }
    #[cfg(any(target_os = "macos", target_os = "freebsd", target_os = "openbsd"))]
    {
        let key = if cfg!(target_os = "macos") {
            "hw.memsize"
        } else {
            "hw.physmem"
        };
        let out = crate::process::Command::new("sysctl")
            .args(["-n", key])
            .force_run()
            .exec()
            .ok()?;
        let bytes: i64 = out.stdout_trimmed().parse().ok()?;
        Some((bytes / (1024 * 1024 * 1024)).max(1))
    }
    #[cfg(not(any(
        target_os = "linux",
        target_os = "macos",
        target_os = "freebsd",
        target_os = "openbsd"
    )))]
    {
        None
    }
}
