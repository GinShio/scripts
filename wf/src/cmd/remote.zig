const std = @import("std");
const cli = @import("../cli.zig");
const log = @import("../core/log.zig");
const yazap = @import("yazap");

pub fn setup(cmd: *yazap.Command) anyerror!void {
    _ = cmd;
}

pub fn execute(allocator: std.mem.Allocator, globals: cli.GlobalOptions, matches: yazap.ArgMatches) !void {
    _ = allocator;
    _ = globals;
    _ = matches;
    std.log.info("Remote module initialized.", .{});
}
