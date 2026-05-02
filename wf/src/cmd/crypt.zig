const std = @import("std");
const cli = @import("../cli.zig");
const log = @import("../core/log.zig");

pub fn execute(allocator: std.mem.Allocator, globals: cli.GlobalOptions, args: *std.process.ArgIterator) !void {
    _ = allocator;
    _ = globals;
    _ = args;
    std.log.info("Crypt module initialized.", .{});
}
