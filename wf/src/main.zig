const std = @import("std");
const cli = @import("cli.zig");
const log = @import("core/log.zig");
const yazap = @import("yazap");

// -----------------------------------------------------------------------------
// Global Configuration
// -----------------------------------------------------------------------------
pub const std_options: std.Options = .{
    .logFn = log.customLogFn,
    .log_level = .debug,
};

pub fn main() !void {
    var gpa = std.heap.GeneralPurposeAllocator(.{}){};
    defer _ = gpa.deinit();
    const allocator = gpa.allocator();

    var app = yazap.App.init(allocator, "wf", "Unified Workflow CLI");
    defer app.deinit();

    var root = app.rootCommand();
    root.setProperty(.help_on_empty_args);

    try root.addArg(yazap.Arg.booleanOption("verbose", 'v', "Enable verbose/debug logging"));
    try root.addArg(yazap.Arg.booleanOption("dry-run", 'n', "Show what would be done without executing"));

    var config_opt = yazap.Arg.singleValueOption("config", 'c', "Path to the TOML configuration file");
    config_opt.setValuePlaceholder("PATH");
    try root.addArg(config_opt);

    // Register subcommands
    for (cli.commands) |cmd_def| {
        var subcmd = app.createCommand(cmd_def.name, cmd_def.description);
        subcmd.setProperty(.help_on_empty_args);
        try cmd_def.setup(&subcmd);
        try root.addSubcommand(subcmd);
    }

    const matches = app.parseProcess() catch |err| {
        std.log.err("Failed to parse arguments: {}", .{err});
        std.process.exit(1);
    };

    const global_opts = cli.GlobalOptions{
        .verbose = matches.containsArg("verbose"),
        .dry_run = matches.containsArg("dry-run"),
        .config_path = matches.getSingleValue("config"),
    };

    log.init(global_opts.verbose, global_opts.dry_run);

    for (cli.commands) |cmd_def| {
        if (matches.subcommandMatches(cmd_def.name)) |sub_matches| {
            try cmd_def.execute(allocator, global_opts, sub_matches);
            return;
        }
    }
}

comptime {
    _ = @import("cli.zig");
    _ = @import("core/log.zig");
    _ = @import("core/config.zig");
    _ = @import("core/process.zig");
    _ = @import("core/git.zig");
    _ = @import("core/crypto.zig");
}
