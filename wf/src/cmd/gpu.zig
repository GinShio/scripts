const std = @import("std");
const cli = @import("../cli.zig");
const log = @import("../core/log.zig");
const yazap = @import("yazap");

pub fn setup(cmd: *yazap.Command) anyerror!void {
    var run_cmd = yazap.Command.init(cmd.allocator, "run", "Run specific GPU tests");
    var tests_arg = yazap.Arg.positional("TESTS", "Test names to run", null);
    tests_arg.setProperty(.takes_multiple_values);
    try run_cmd.addArg(tests_arg);
    try cmd.addSubcommand(run_cmd);

    var install_cmd = yazap.Command.init(cmd.allocator, "install", "Install test suites (Toolbox)");
    var targets_arg = yazap.Arg.positional("TARGETS", "Targets to install", null);
    targets_arg.setProperty(.takes_multiple_values);
    try install_cmd.addArg(targets_arg);
    try cmd.addSubcommand(install_cmd);

    const restore_cmd = yazap.Command.init(cmd.allocator, "restore", "Restore baseline results");
    try cmd.addSubcommand(restore_cmd);

    const cleanup_cmd = yazap.Command.init(cmd.allocator, "cleanup", "Cleanup old results");
    try cmd.addSubcommand(cleanup_cmd);

    const list_cmd = yazap.Command.init(cmd.allocator, "list", "List drivers or suites");
    try cmd.addSubcommand(list_cmd);
}

pub fn execute(allocator: std.mem.Allocator, globals: cli.GlobalOptions, matches: yazap.ArgMatches) !void {
    _ = allocator;

    if (matches.subcommandMatches("run")) |sub_matches| {
        try runTests(globals, sub_matches);
    } else if (matches.subcommandMatches("install")) |sub_matches| {
        try runInstall(globals, sub_matches);
    } else if (matches.subcommandMatches("restore")) |_| {
        std.log.info("Restoring baseline results...", .{});
    } else if (matches.subcommandMatches("cleanup")) |_| {
        std.log.info("Cleaning up old results...", .{});
    } else if (matches.subcommandMatches("list")) |_| {
        std.log.info("Listing drivers or suites...", .{});
    } else {
        std.log.err("The 'gpu' command requires a subcommand.", .{});
        std.process.exit(1);
    }
}

fn runTests(globals: cli.GlobalOptions, matches: yazap.ArgMatches) !void {
    _ = globals;
    std.log.info("Starting GPU tests...", .{});

    log.dryRun("Would execute test runner", .{});

    if (matches.getMultiValues("TESTS")) |tests| {
        for (tests) |test_name| {
            std.log.debug("Queueing test: {s}", .{test_name});
        }
    }
}

fn runInstall(globals: cli.GlobalOptions, matches: yazap.ArgMatches) !void {
    _ = globals;
    std.log.info("Installing GPU test toolbox...", .{});

    if (matches.getMultiValues("TARGETS")) |targets| {
        for (targets) |target| {
            std.log.debug("Will install target: {s}", .{target});
        }
    }
}
