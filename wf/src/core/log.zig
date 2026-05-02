const std = @import("std");

pub var is_verbose: bool = false;
pub var is_dry_run: bool = false;

/// Initializes the logging system states.
pub fn init(verbose: bool, dry_run: bool) void {
    is_verbose = verbose;
    is_dry_run = dry_run;
}

/// A custom log function designed to be hooked into `std.options.logFn`.
/// This allows us to intercept all `std.log` calls (including those from
/// third-party libraries) and format them uniformly.
pub fn customLogFn(
    comptime level: std.log.Level,
    comptime scope: @TypeOf(.EnumLiteral),
    comptime format: []const u8,
    args: anytype,
) void {
    // Filter out debug logs unless verbose mode is enabled
    if (level == .debug and !is_verbose) return;

    // Format the scope prefix (e.g., "[INFO] (builder): ...")
    const scope_prefix = if (scope == .default) "" else "(" ++ @tagName(scope) ++ ") ";
    const level_txt = comptime level.asText();

    // Convert level text to uppercase for consistency
    var upper_level: [level_txt.len]u8 = undefined;
    for (level_txt, 0..) |c, i| {
        upper_level[i] = std.ascii.toUpper(c);
    }

    const stderr = std.io.getStdErr().writer();

    // We use a mutex to ensure thread-safe logging if we ever use threads
    std.debug.lockStdErr();
    defer std.debug.unlockStdErr();

    // Since upper_level is an array, we need to slice it to print it as a string
    stderr.print("[{s}] {s}", .{ upper_level[0..], scope_prefix }) catch return;
    stderr.print(format ++ "\n", args) catch return;
}

/// Specialized function for logging dry-run actions.
/// Dry-run logs go to stdout instead of stderr, as they are often the primary
/// expected output when `-n` is passed.
pub fn dryRun(comptime format: []const u8, args: anytype) void {
    if (is_dry_run) {
        const stdout = std.io.getStdOut().writer();
        stdout.print("[DRY-RUN] " ++ format ++ "\n", args) catch return;
    }
}

// -----------------------------------------------------------------------------
// Unit Tests
// -----------------------------------------------------------------------------

test "logger state initialization" {
    init(true, false);
    try std.testing.expect(is_verbose == true);
    try std.testing.expect(is_dry_run == false);
}
