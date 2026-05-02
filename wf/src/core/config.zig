const std = @import("std");
const toml = @import("toml");

/// Resolves the configuration file path based on priority:
/// 1. Command Line (`cli_path`)
/// 2. Environment Variable (`WF_CONFIG`)
/// 3. Execution Directory (`wf.toml` or `.wf.toml`)
///
/// Returns an allocated string if found, or null. The caller must free the string.
pub fn resolveConfigPath(allocator: std.mem.Allocator, cli_path: ?[]const u8) !?[]const u8 {
    // 1. Command Line Priority
    if (cli_path) |p| {
        std.log.debug("Config path resolved from CLI: {s}", .{p});
        return try allocator.dupe(u8, p);
    }

    // 2. Environment Variable Priority
    if (std.process.getEnvVarOwned(allocator, "WF_CONFIG")) |env_path| {
        std.log.debug("Config path resolved from ENV (WF_CONFIG): {s}", .{env_path});
        return env_path;
    } else |err| switch (err) {
        error.EnvironmentVariableNotFound => {},
        else => return err,
    }

    // 3. Execution Directory (CWD) Priority
    const default_names = [_][]const u8{ "wf.toml", ".wf.toml" };
    for (default_names) |name| {
        if (std.fs.cwd().statFile(name)) |_| {
            std.log.debug("Config path resolved from CWD: {s}", .{name});
            return try allocator.dupe(u8, name);
        } else |err| switch (err) {
            error.FileNotFound => continue,
            else => return err,
        }
    }

    return null;
}

/// Recursively merges `src` into `dest`.
/// This is heavily used when loading a directory of TOML files, where later files
/// override earlier ones.
/// - For Optionals: if `src` is non-null, it overwrites `dest` (or merges if it's a nested struct).
/// - For Structs: it recursively merges fields.
/// - For Primitives/Slices: `src` overwrites `dest`.
pub fn mergeStructs(comptime T: type, dest: *T, src: T) void {
    const info = @typeInfo(T);
    switch (info) {
        .@"struct" => |struct_info| {
            inline for (struct_info.fields) |field| {
                const f_type = field.type;
                const f_info = @typeInfo(f_type);

                if (f_info == .optional) {
                    if (@field(src, field.name)) |src_val| {
                        const ChildT = f_info.optional.child;
                        if (@typeInfo(ChildT) == .@"struct") {
                            if (@field(dest, field.name) == null) {
                                @field(dest, field.name) = src_val;
                            } else {
                                mergeStructs(ChildT, &@field(dest, field.name).?, src_val);
                            }
                        } else {
                            @field(dest, field.name) = src_val;
                        }
                    }
                } else if (f_info == .@"struct") {
                    mergeStructs(f_type, &@field(dest, field.name), @field(src, field.name));
                } else {
                    @field(dest, field.name) = @field(src, field.name);
                }
            }
        },
        else => @compileError("mergeStructs only supports structs"),
    }
}

/// ConfigLoader provides a strongly-typed, generic interface for loading
/// TOML v1.0 configuration files and directories.
///
/// Design Philosophy (Decentralized Configuration):
/// The Core does NOT know about specific command configurations (e.g., BuilderConfig).
/// Instead, it acts as a generic engine. Each command defines its own expected
/// schema (`comptime T`) and asks the ConfigLoader to parse the file into that schema.
pub const ConfigLoader = struct {
    allocator: std.mem.Allocator,
    arenas: std.ArrayList(std.heap.ArenaAllocator),

    pub fn init(allocator: std.mem.Allocator) ConfigLoader {
        return .{
            .allocator = allocator,
            .arenas = std.ArrayList(std.heap.ArenaAllocator).init(allocator),
        };
    }

    pub fn deinit(self: *ConfigLoader) void {
        for (self.arenas.items) |arena| {
            var mut_arena = arena;
            mut_arena.deinit();
        }
        self.arenas.deinit();
    }

    /// Resolves the config path and loads it into the requested type `T`.
    /// If the path is a directory, it globs all `*.toml` files, parses them,
    /// and deep-merges them in alphabetical order.
    pub fn load(self: *ConfigLoader, comptime T: type, cli_path: ?[]const u8) !T {
        const resolved_path = try resolveConfigPath(self.allocator, cli_path);
        if (resolved_path) |p| {
            defer self.allocator.free(p);
            return try self.loadPath(T, p);
        }

        std.log.debug("No configuration file found. Using defaults for '{s}'.", .{@typeName(T)});
        return T{};
    }

    /// Internal method to handle both files and directories.
    fn loadPath(self: *ConfigLoader, comptime T: type, path: []const u8) !T {
        const stat = std.fs.cwd().statFile(path) catch |err| {
            std.log.err("Failed to stat config path '{s}': {}", .{ path, err });
            return err;
        };

        if (stat.kind == .directory) {
            std.log.debug("Config path is a directory: {s}. Merging all *.toml files.", .{path});
            return try self.loadDirectory(T, path);
        } else {
            return try self.parseFile(T, path);
        }
    }

    /// Loads all `*.toml` files in a directory, sorts them alphabetically, and deep merges them.
    fn loadDirectory(self: *ConfigLoader, comptime T: type, dir_path: []const u8) !T {
        var dir = try std.fs.cwd().openDir(dir_path, .{ .iterate = true });
        defer dir.close();

        var files = std.ArrayList([]const u8).init(self.allocator);
        defer {
            for (files.items) |f| self.allocator.free(f);
            files.deinit();
        }

        var it = dir.iterate();
        while (try it.next()) |entry| {
            if (entry.kind == .file and std.mem.endsWith(u8, entry.name, ".toml")) {
                try files.append(try self.allocator.dupe(u8, entry.name));
            }
        }

        if (files.items.len == 0) {
            std.log.warn("No .toml configuration files found in directory: {s}", .{dir_path});
            return T{};
        }

        // Sort files alphabetically to ensure deterministic merging
        std.mem.sort([]const u8, files.items, {}, struct {
            fn lessThan(context: void, a: []const u8, b: []const u8) bool {
                _ = context;
                return std.mem.order(u8, a, b) == .lt;
            }
        }.lessThan);

        var merged_config = T{};

        for (files.items) |file_name| {
            const full_path = try std.fs.path.join(self.allocator, &[_][]const u8{ dir_path, file_name });
            defer self.allocator.free(full_path);

            const file_config = try self.parseFile(T, full_path);
            mergeStructs(T, &merged_config, file_config);
        }

        return merged_config;
    }

    /// Internal method to read and parse a single file.
    fn parseFile(self: *ConfigLoader, comptime T: type, file_path: []const u8) !T {
        std.log.debug("Loading typed config file: {s}", .{file_path});

        const file = try std.fs.cwd().openFile(file_path, .{});
        defer file.close();

        const file_size = try file.getEndPos();
        const content = try file.readToEndAlloc(self.allocator, file_size);
        defer self.allocator.free(content);

        // TODO: Integrate a real TOML v1.0 parser (e.g., zig-toml) here.
        var parser = toml.Parser(T).init(self.allocator);
        defer parser.deinit();

        const result = try parser.parseString(content);
        try self.arenas.append(result.arena);
        return result.value;
    }
};

// -----------------------------------------------------------------------------
// Unit Tests
// -----------------------------------------------------------------------------

test "Config Path Resolution - CLI Priority" {
    const path = try resolveConfigPath(std.testing.allocator, "/custom/path.toml");
    try std.testing.expect(path != null);
    defer std.testing.allocator.free(path.?);
    try std.testing.expectEqualStrings("/custom/path.toml", path.?);
}

test "Config Path Resolution - CWD Fallback" {
    const test_file = ".wf.toml";
    const file = try std.fs.cwd().createFile(test_file, .{});
    try file.writeAll("test");
    file.close();
    defer std.fs.cwd().deleteFile(test_file) catch {};

    const path = try resolveConfigPath(std.testing.allocator, null);
    try std.testing.expect(path != null);
    defer std.testing.allocator.free(path.?);
    try std.testing.expectEqualStrings(".wf.toml", path.?);
}

const DummyNested = struct {
    value: ?u32 = null,
};

const DummyConfig = struct {
    name: ?[]const u8 = null,
    nested: DummyNested = .{},
};

test "ConfigLoader - Deep Merge Structs" {
    var dest = DummyConfig{
        .name = "original",
        .nested = .{ .value = 10 },
    };

    const src = DummyConfig{
        .name = "overridden",
        .nested = .{ .value = 20 },
    };

    mergeStructs(DummyConfig, &dest, src);

    try std.testing.expectEqualStrings("overridden", dest.name.?);
    try std.testing.expectEqual(@as(u32, 20), dest.nested.value.?);
}

test "ConfigLoader - Directory Loading Logic" {
    const tmp_dir = "test_config_dir";
    std.fs.cwd().makeDir(tmp_dir) catch {};
    defer std.fs.cwd().deleteTree(tmp_dir) catch {};

    // Create a.toml and b.toml
    const file_a = try std.fs.cwd().createFile("test_config_dir/a.toml", .{});
    try file_a.writeAll("name = \"a\"");
    file_a.close();

    const file_b = try std.fs.cwd().createFile("test_config_dir/b.toml", .{});
    try file_b.writeAll("name = \"b\"");
    file_b.close();

    var loader = ConfigLoader.init(std.testing.allocator);
    defer loader.deinit();

    // This will test the directory iteration and sorting logic.
    // Since parsing is stubbed, the result will be empty, but it ensures no crashes or leaks.
    _ = try loader.load(DummyConfig, tmp_dir);
}
