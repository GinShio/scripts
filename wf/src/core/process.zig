const std = @import("std");
const log = @import("log.zig");

/// Represents the outcome of an executed command.
/// The caller is responsible for calling `deinit` to free stdout/stderr.
pub const CommandResult = struct {
    allocator: std.mem.Allocator,
    term: std.process.Child.Term,
    stdout: []const u8,
    stderr: []const u8,

    pub fn deinit(self: *const CommandResult) void {
        self.allocator.free(self.stdout);
        self.allocator.free(self.stderr);
    }

    /// Returns true if the command exited normally with code 0.
    pub fn isSuccess(self: *const CommandResult) bool {
        return switch (self.term) {
            .Exited => |code| code == 0,
            else => false,
        };
    }
};

/// A fluent builder for orchestrating shell commands.
/// Integrates seamlessly with the global dry-run and logging systems.
pub const Command = struct {
    allocator: std.mem.Allocator,
    argv: std.ArrayList([]const u8),
    cwd: ?[]const u8 = null,
    env_map: ?*const std.process.EnvMap = null,
    ignore_dry_run: bool = false,

    /// Initializes a new command with the given executable.
    pub fn init(allocator: std.mem.Allocator, exe: []const u8) Command {
        var argv = std.ArrayList([]const u8).init(allocator);
        argv.append(exe) catch @panic("OOM");
        return .{
            .allocator = allocator,
            .argv = argv,
        };
    }

    pub fn deinit(self: *Command) void {
        self.argv.deinit();
    }

    /// Appends a single argument to the command.
    pub fn arg(self: *Command, a: []const u8) *Command {
        self.argv.append(a) catch @panic("OOM");
        return self;
    }

    /// Appends multiple arguments to the command.
    pub fn args(self: *Command, as: []const []const u8) *Command {
        self.argv.appendSlice(as) catch @panic("OOM");
        return self;
    }

    /// Sets the working directory for the command.
    pub fn setCwd(self: *Command, path: []const u8) *Command {
        self.cwd = path;
        return self;
    }

    /// Sets a custom environment map for the command.
    pub fn setEnvMap(self: *Command, env: *const std.process.EnvMap) *Command {
        self.env_map = env;
        return self;
    }

    /// Forces the command to run even if global dry-run is enabled.
    /// Useful for read-only commands like `git status` that are needed for orchestration.
    pub fn forceRun(self: *Command) *Command {
        self.ignore_dry_run = true;
        return self;
    }

    /// Formats the command into a string for logging purposes.
    fn formatCmd(self: *const Command, writer: anytype) !void {
        for (self.argv.items, 0..) |a, i| {
            if (i > 0) try writer.writeByte(' ');
            // Simple quoting for display purposes
            if (std.mem.indexOfScalar(u8, a, ' ') != null) {
                try writer.print("\"{s}\"", .{a});
            } else {
                try writer.print("{s}", .{a});
            }
        }
        if (self.cwd) |cwd| {
            try writer.print(" (cwd={s})", .{cwd});
        }
    }

    /// Executes the command, capturing stdout and stderr.
    pub fn exec(self: *Command) !CommandResult {
        if (log.is_dry_run and !self.ignore_dry_run) {
            var buf: [4096]u8 = undefined;
            var fba = std.heap.FixedBufferAllocator.init(&buf);
            var str = std.ArrayList(u8).init(fba.allocator());
            try self.formatCmd(str.writer());
            log.dryRun("{s}", .{str.items});
            return CommandResult{
                .allocator = self.allocator,
                .term = .{ .Exited = 0 },
                .stdout = try self.allocator.alloc(u8, 0),
                .stderr = try self.allocator.alloc(u8, 0),
            };
        }

        if (log.is_verbose) {
            var buf: [4096]u8 = undefined;
            var fba = std.heap.FixedBufferAllocator.init(&buf);
            var str = std.ArrayList(u8).init(fba.allocator());
            try self.formatCmd(str.writer());
            std.log.debug("Executing: {s}", .{str.items});
        }

        var child = std.process.Child.init(self.argv.items, self.allocator);
        child.cwd = self.cwd;
        if (self.env_map) |env| {
            child.env_map = env;
        }

        child.stdin_behavior = .Ignore;
        child.stdout_behavior = .Pipe;
        child.stderr_behavior = .Pipe;

        try child.spawn();

        const stdout_max = 10 * 1024 * 1024; // 10MB limit
        const stderr_max = 10 * 1024 * 1024; // 10MB limit
        _ = stderr_max; // TODO: handle stderr max if needed by Zig API, otherwise remove

        var stdout = std.ArrayListUnmanaged(u8){};
        errdefer stdout.deinit(self.allocator);
        var stderr = std.ArrayListUnmanaged(u8){};
        errdefer stderr.deinit(self.allocator);

        try child.collectOutput(self.allocator, &stdout, &stderr, stdout_max);

        const term = try child.wait();

        return CommandResult{
            .allocator = self.allocator,
            .term = term,
            .stdout = try stdout.toOwnedSlice(self.allocator),
            .stderr = try stderr.toOwnedSlice(self.allocator),
        };
    }

    /// Executes the command and checks the exit code.
    /// If the command fails, it logs the stderr and returns `error.CommandFailed`.
    pub fn execCheck(self: *Command) !CommandResult {
        const res = try self.exec();
        if (!res.isSuccess()) {
            if (res.stderr.len > 0) {
                std.log.err("Command failed: {s}", .{res.stderr});
            } else if (res.stdout.len > 0) {
                std.log.err("Command failed: {s}", .{res.stdout});
            }
            res.deinit();
            return error.CommandFailed;
        }
        return res;
    }

    /// Executes the command, streaming stdout and stderr to the parent process.
    pub fn stream(self: *Command) !CommandResult {
        if (log.is_dry_run and !self.ignore_dry_run) {
            var buf: [4096]u8 = undefined;
            var fba = std.heap.FixedBufferAllocator.init(&buf);
            var str = std.ArrayList(u8).init(fba.allocator());
            try self.formatCmd(str.writer());
            log.dryRun("{s} [streamed]", .{str.items});
            return CommandResult{
                .allocator = self.allocator,
                .term = .{ .Exited = 0 },
                .stdout = try self.allocator.alloc(u8, 0),
                .stderr = try self.allocator.alloc(u8, 0),
            };
        }

        if (log.is_verbose) {
            var buf: [4096]u8 = undefined;
            var fba = std.heap.FixedBufferAllocator.init(&buf);
            var str = std.ArrayList(u8).init(fba.allocator());
            try self.formatCmd(str.writer());
            std.log.debug("Streaming: {s}", .{str.items});
        }

        var child = std.process.Child.init(self.argv.items, self.allocator);
        child.cwd = self.cwd;
        if (self.env_map) |env| {
            child.env_map = env;
        }

        child.stdin_behavior = .Inherit;
        child.stdout_behavior = .Inherit;
        child.stderr_behavior = .Inherit;

        const term = try child.spawnAndWait();

        return CommandResult{
            .allocator = self.allocator,
            .term = term,
            .stdout = try self.allocator.alloc(u8, 0),
            .stderr = try self.allocator.alloc(u8, 0),
        };
    }

    /// Streams the command and checks the exit code.
    pub fn streamCheck(self: *Command) !CommandResult {
        const res = try self.stream();
        if (!res.isSuccess()) {
            std.log.err("Streamed command failed.", .{});
            res.deinit();
            return error.CommandFailed;
        }
        return res;
    }
};

// -----------------------------------------------------------------------------
// Unit Tests
// -----------------------------------------------------------------------------

test "Command builder and exec" {
    var cmd = Command.init(std.testing.allocator, "echo");
    defer cmd.deinit();

    _ = cmd.arg("hello").arg("world");

    const res = try cmd.execCheck();
    defer res.deinit();

    try std.testing.expect(res.isSuccess());
    try std.testing.expectEqualStrings("hello world\n", res.stdout);
}

test "Command dry-run behavior" {
    log.init(false, true); // Enable dry-run
    defer log.init(false, false); // Reset

    var cmd = Command.init(std.testing.allocator, "false");
    defer cmd.deinit();

    // In dry-run, 'false' will return success and not actually run
    const res = try cmd.execCheck();
    defer res.deinit();

    try std.testing.expect(res.isSuccess());
}
