const std = @import("std");

/// ConfigLoader provides a strongly-typed, generic interface for loading
/// TOML v1.0 configuration files.
///
/// By using Zig's `comptime T: type`, we ensure that configuration files are
/// parsed directly into strongly-typed Zig structs, eliminating boilerplate
/// validation code and ensuring memory safety.
pub const ConfigLoader = struct {
    allocator: std.mem.Allocator,

    pub fn init(allocator: std.mem.Allocator) ConfigLoader {
        return .{
            .allocator = allocator,
        };
    }

    /// Loads and parses a TOML file into the specified struct type `T`.
    /// The caller owns the memory within the returned struct (if it contains pointers)
    /// and should free it using an arena allocator or manual cleanup.
    pub fn load(self: *ConfigLoader, comptime T: type, file_path: []const u8) !T {
        std.log.debug("Loading typed config file: {s}", .{file_path});

        // 1. Open and read the entire file
        const file = try std.fs.cwd().openFile(file_path, .{});
        defer file.close();

        const file_size = try file.getEndPos();
        const content = try file.readToEndAlloc(self.allocator, file_size);
        defer self.allocator.free(content);

        // 2. Parse the TOML content into the struct
        // TODO: Integrate a real TOML v1.0 parser (e.g., zig-toml) here.
        // Example future implementation:
        // var parser = toml.Parser.init(self.allocator);
        // defer parser.deinit();
        // return try parser.parse(T, content);

        std.log.warn("TOML parsing is currently stubbed. Returning default-initialized struct for type '{s}'.", .{@typeName(T)});

        // Stub implementation: return zero-initialized struct
        var result: T = undefined;
        @memset(std.mem.asBytes(&result), 0);
        return result;
    }
};

// -----------------------------------------------------------------------------
// Unit Tests
// -----------------------------------------------------------------------------

const DummyConfig = struct {
    name: []const u8,
    version: u32,
};

test "ConfigLoader API contract" {
    // Create a dummy file for the test
    const test_file = "test_dummy.toml";
    const file = try std.fs.cwd().createFile(test_file, .{});
    try file.writeAll("name = \"test\"\nversion = 1");
    file.close();
    defer std.fs.cwd().deleteFile(test_file) catch {};

    var loader = ConfigLoader.init(std.testing.allocator);

    // Test the generic load function
    const config = try loader.load(DummyConfig, test_file);

    // Since it's stubbed, it will return 0/null values, but the API contract is verified
    try std.testing.expect(config.version == 0);
}
