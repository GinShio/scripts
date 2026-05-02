const std = @import("std");
const cli = @import("../cli.zig");
const log = @import("../core/log.zig");

pub fn execute(allocator: std.mem.Allocator, globals: cli.GlobalOptions, args: *std.process.ArgIterator) !void {
    _ = allocator;

    const subcmd = args.next() orelse {
        std.log.err("The 'gpu' command requires a subcommand.", .{});
        printHelp();
        std.process.exit(1);
    };

    if (std.mem.eql(u8, subcmd, "run")) {
        try runTests(globals, args);
    } else if (std.mem.eql(u8, subcmd, "install")) {
        try runInstall(globals, args);
    } else {
        std.log.err("Unknown 'gpu' subcommand: '{s}'", .{subcmd});
        printHelp();
        std.process.exit(1);
    }
}

fn runTests(globals: cli.GlobalOptions, args: *std.process.ArgIterator) !void {
    _ = globals;
    std.log.info("Starting GPU tests...", .{});

    log.dryRun("Would execute test runner", .{});

    while (args.next()) |test_name| {
        std.log.debug("Queueing test: {s}", .{test_name});
    }
}

fn runInstall(globals: cli.GlobalOptions, args: *std.process.ArgIterator) !void {
    _ = globals;
    std.log.info("Installing GPU test toolbox...", .{});

    while (args.next()) |target| {
        std.log.debug("Will install target: {s}", .{target});
    }
}

fn printHelp() void {
    const stdout = std.io.getStdOut().writer();
    stdout.print(
        \\Usage: wf gpu <subcommand> [args...]
        \\
        \\GPU Test Automation Tool
        \\
        \\Subcommands:
        \\  run <tests...>    Run specific GPU tests
        \\  install           Install test suites (Toolbox)
        \\  restore           Restore baseline results
        \\  cleanup           Cleanup old results
        \\  list              List drivers or suites
        \\
    , .{}) catch {};
}
