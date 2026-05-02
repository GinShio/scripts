const std = @import("std");
const log = @import("core/log.zig");

/// GlobalOptions holds the configuration that applies to the entire CLI.
pub const GlobalOptions = struct {
    verbose: bool = false,
    dry_run: bool = false,
    config_path: ?[]const u8 = null,
};

/// Defines the signature for all subcommand execution functions.
pub const CommandFn = *const fn (allocator: std.mem.Allocator, globals: GlobalOptions, args: *std.process.ArgIterator) anyerror!void;

/// Represents a registered CLI subcommand.
pub const Command = struct {
    name: []const u8,
    description: []const u8,
    execute: CommandFn,
};

/// The Command Registry.
/// To add a new command to the CLI, simply add a new entry to this array.
/// This makes the CLI highly extensible and maintainable.
pub const commands = [_]Command{
    .{ .name = "build", .description = "Branch-aware build orchestration", .execute = @import("cmd/builder.zig").execute },
    .{ .name = "stack", .description = "Stacked PRs and remote sync", .execute = @import("cmd/stack.zig").execute },
    .{ .name = "gpu", .description = "GPU test automation tool", .execute = @import("cmd/gpu.zig").execute },
    .{ .name = "remote", .description = "Git remotes setup and mirroring", .execute = @import("cmd/remote.zig").execute },
    .{ .name = "crypt", .description = "Transparent file encryption", .execute = @import("cmd/crypt.zig").execute },
};

/// Parses global options from the argument iterator until a subcommand is found.
pub fn parseGlobalOptions(args: *std.process.ArgIterator, opts: *GlobalOptions) !?[]const u8 {
    while (args.next()) |arg| {
        if (std.mem.eql(u8, arg, "--verbose") or std.mem.eql(u8, arg, "-v")) {
            opts.verbose = true;
        } else if (std.mem.eql(u8, arg, "--dry-run") or std.mem.eql(u8, arg, "-n")) {
            opts.dry_run = true;
        } else if (std.mem.eql(u8, arg, "--config") or std.mem.eql(u8, arg, "-c")) {
            opts.config_path = args.next();
            if (opts.config_path == null) {
                std.log.err("Option '--config' requires an argument.", .{});
                std.process.exit(1);
            }
        } else if (std.mem.eql(u8, arg, "--help") or std.mem.eql(u8, arg, "-h")) {
            printHelp();
            std.process.exit(0);
        } else if (std.mem.startsWith(u8, arg, "-")) {
            std.log.err("Unknown global option: '{s}'", .{arg});
            std.process.exit(1);
        } else {
            // First non-flag argument is the subcommand
            return arg;
        }
    }
    return null;
}

/// Routes the execution to the appropriate command from the registry.
pub fn executeCommand(allocator: std.mem.Allocator, name: []const u8, globals: GlobalOptions, args: *std.process.ArgIterator) !void {
    for (commands) |cmd| {
        if (std.mem.eql(u8, cmd.name, name)) {
            return cmd.execute(allocator, globals, args);
        }
    }

    std.log.err("Unknown command: '{s}'", .{name});
    printHelp();
    std.process.exit(1);
}

/// Prints the dynamically generated global help message.
pub fn printHelp() void {
    const stdout = std.io.getStdOut().writer();

    stdout.print("Usage: wf [global options] <command> [args...]\n\n", .{}) catch {};
    stdout.print("Global Options:\n", .{}) catch {};
    stdout.print("  -v, --verbose    Enable verbose/debug logging\n", .{}) catch {};
    stdout.print("  -n, --dry-run    Show what would be done without executing\n", .{}) catch {};
    stdout.print("  -c, --config     Path to the TOML configuration file\n\n", .{}) catch {};

    stdout.print("Commands:\n", .{}) catch {};
    for (commands) |cmd| {
        // Dynamically format the command list based on the registry
        stdout.print("  {s:<16} {s}\n", .{ cmd.name, cmd.description }) catch {};
    }

    stdout.print("\nUse 'wf <command> --help' for more information on a specific command.\n", .{}) catch {};
}

// -----------------------------------------------------------------------------
// Unit Tests
// -----------------------------------------------------------------------------

test "GlobalOptions defaults" {
    const opts = GlobalOptions{};
    try std.testing.expect(opts.verbose == false);
    try std.testing.expect(opts.dry_run == false);
    try std.testing.expect(opts.config_path == null);
}
