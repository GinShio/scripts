const std = @import("std");
const cli = @import("cli.zig");
const log = @import("core/log.zig");

// -----------------------------------------------------------------------------
// Global Configuration
// -----------------------------------------------------------------------------
// We hook Zig's standard logging system to our custom logger.
// This ensures that `std.log.info`, `std.log.err`, etc., (even from third-party
// libraries) are correctly formatted and respect our global verbose/dry-run flags.
pub const std_options: std.Options = .{
    .logFn = log.customLogFn,
    .log_level = .debug, // Ensure all logs reach customLogFn, we filter them dynamically
};

/// The main entry point for the Unified Workflow CLI.
pub fn main() !void {
    // Initialize the General Purpose Allocator
    var gpa = std.heap.GeneralPurposeAllocator(.{}){};
    defer _ = gpa.deinit();
    const allocator = gpa.allocator();

    // Parse command line arguments
    var args = try std.process.argsWithAllocator(allocator);
    defer args.deinit();

    // Skip the executable name
    _ = args.skip();

    // Parse global options and extract the subcommand
    var global_opts = cli.GlobalOptions{};
    const cmd_name = try cli.parseGlobalOptions(&args, &global_opts);

    // Initialize the logging state based on global options
    log.init(global_opts.verbose, global_opts.dry_run);

    // Route to the appropriate subcommand via the CLI registry
    if (cmd_name) |cmd| {
        try cli.executeCommand(allocator, cmd, global_opts, &args);
    } else {
        // No subcommand provided
        cli.printHelp();
    }
}

// This block ensures that `zig test src/main.zig` will discover and run
// tests in all imported modules.
comptime {
    _ = @import("cli.zig");
    _ = @import("core/log.zig");
    _ = @import("core/config.zig");
    _ = @import("core/process.zig");
    _ = @import("core/git.zig");
}
