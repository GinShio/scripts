const std = @import("std");
const log = @import("core/log.zig");
const git = @import("core/git.zig");
const yazap = @import("yazap");

/// GlobalOptions holds the configuration that applies to the entire CLI.
pub const GlobalOptions = struct {
    verbose: bool = false,
    dry_run: bool = false,
    config_path: ?[]const u8 = null,
};

/// Defines the signature for all subcommand execution functions.
pub const CommandFn = *const fn (allocator: std.mem.Allocator, globals: GlobalOptions, matches: yazap.ArgMatches) anyerror!void;

/// Represents a registered CLI subcommand.
pub const Command = struct {
    name: []const u8,
    description: []const u8,
    setup: *const fn (cmd: *yazap.Command) anyerror!void,
    execute: CommandFn,
};

/// The Command Registry.
pub const commands = [_]Command{
    .{ .name = "build", .description = "Branch-aware build orchestration", .setup = @import("cmd/builder.zig").setup, .execute = @import("cmd/builder.zig").execute },
    .{ .name = "stack", .description = "Stacked PRs and remote sync", .setup = @import("cmd/stack.zig").setup, .execute = @import("cmd/stack.zig").execute },
    .{ .name = "gpu", .description = "GPU test automation tool", .setup = @import("cmd/gpu.zig").setup, .execute = @import("cmd/gpu.zig").execute },
    .{ .name = "remote", .description = "Git remotes setup and mirroring", .setup = @import("cmd/remote.zig").setup, .execute = @import("cmd/remote.zig").execute },
    .{ .name = "crypt", .description = "Transparent file encryption", .setup = @import("cmd/crypt.zig").setup, .execute = @import("cmd/crypt.zig").execute },
};

// -----------------------------------------------------------------------------
// Resolver (Configuration Resolution Chain)
// -----------------------------------------------------------------------------

/// Identifies the source of a configuration value, useful for printing status
pub const ConfigSource = enum {
    cli,
    env,
    git,
    toml,
    default,
};

/// Represents a resolved value along with its source
pub const ResolvedValue = struct {
    value: []const u8,
    source: ConfigSource,
};

/// A hierarchical configuration resolver.
pub const Resolver = struct {
    allocator: std.mem.Allocator,
    repo: ?*const git.Repository,
    prefix: []const u8,
    context: ?[]const u8,
    cli_args: ?std.StringHashMap([]const u8) = null,

    pub fn init(allocator: std.mem.Allocator, repo: ?*const git.Repository, prefix: []const u8, context: ?[]const u8) Resolver {
        return .{
            .allocator = allocator,
            .repo = repo,
            .prefix = prefix,
            .context = context,
        };
    }

    pub fn deinit(self: *Resolver) void {
        if (self.cli_args) |*args| {
            args.deinit();
        }
    }

    /// Set a CLI argument override
    pub fn setCliArg(self: *Resolver, key: []const u8, value: []const u8) !void {
        if (self.cli_args == null) {
            self.cli_args = std.StringHashMap([]const u8).init(self.allocator);
        }
        try self.cli_args.?.put(key, value);
    }

    /// Retrieves a value for a given key by probing the resolution chain.
    pub fn get(self: *const Resolver, key: []const u8) !?ResolvedValue {
        // 1. Check CLI arguments
        if (self.cli_args) |args| {
            if (args.get(key)) |val| {
                return ResolvedValue{ .value = val, .source = .cli };
            }
        }

        // 2. Check Environment Variables (with Context)
        if (self.context) |ctx| {
            const is_default = std.mem.eql(u8, ctx, "default");

            // Try Context env var: PREFIX_CTX_KEY
            if (try self.getEnv(ctx, key)) |val| {
                return ResolvedValue{ .value = val, .source = .env };
            }

            // 3. Try fallback Environment Variable without context
            if (is_default) {
                if (try self.getEnv(null, key)) |val| {
                    return ResolvedValue{ .value = val, .source = .env };
                }
            }
        } else {
            // No context specified, try direct
            if (try self.getEnv(null, key)) |val| {
                return ResolvedValue{ .value = val, .source = .env };
            }
        }

        // 4. Check Git Config (if repository is available)
        if (self.repo) |r| {
            if (self.context) |ctx| {
                const is_default = std.mem.eql(u8, ctx, "default");

                // Try Context Git Config: prefix.ctx.key
                if (try self.getGitConfig(r, ctx, key)) |val| {
                    return ResolvedValue{ .value = val, .source = .git };
                }

                // 5. Try fallback Git Config without context
                if (is_default) {
                    if (try self.getGitConfig(r, null, key)) |val| {
                        return ResolvedValue{ .value = val, .source = .git };
                    }
                }
            } else {
                if (try self.getGitConfig(r, null, key)) |val| {
                    return ResolvedValue{ .value = val, .source = .git };
                }
            }
        }

        return null;
    }

    /// Helper to get a string value, returning the default if not found
    pub fn getStringOrDefault(self: *const Resolver, key: []const u8, default_val: []const u8) !ResolvedValue {
        if (try self.get(key)) |res| {
            return res;
        }
        return ResolvedValue{ .value = default_val, .source = .default };
    }

    /// Internal: Gets an environment variable, formats name as PREFIX_[CTX_]KEY (all uppercase)
    fn getEnv(self: *const Resolver, ctx: ?[]const u8, key: []const u8) !?[]const u8 {
        var env_name_raw: []const u8 = undefined;
        if (ctx) |c| {
            env_name_raw = try std.fmt.allocPrint(self.allocator, "{s}_{s}_{s}", .{ self.prefix, c, key });
        } else {
            env_name_raw = try std.fmt.allocPrint(self.allocator, "{s}_{s}", .{ self.prefix, key });
        }
        defer self.allocator.free(env_name_raw);

        // Convert to uppercase
        const env_name = try self.allocator.alloc(u8, env_name_raw.len);
        defer self.allocator.free(env_name);
        for (env_name_raw, 0..) |c, i| {
            env_name[i] = std.ascii.toUpper(c);
        }

        if (std.process.getEnvVarOwned(self.allocator, env_name)) |val| {
            return val;
        } else |err| switch (err) {
            error.EnvironmentVariableNotFound => return null,
            else => return err,
        }
    }

    /// Internal: Gets a Git config variable, formats name as prefix.[ctx.]key (all lowercase)
    fn getGitConfig(self: *const Resolver, r: *const git.Repository, ctx: ?[]const u8, key: []const u8) !?[]const u8 {
        var git_key_raw: []const u8 = undefined;
        if (ctx) |c| {
            git_key_raw = try std.fmt.allocPrint(self.allocator, "{s}.{s}.{s}", .{ self.prefix, c, key });
        } else {
            git_key_raw = try std.fmt.allocPrint(self.allocator, "{s}.{s}", .{ self.prefix, key });
        }
        defer self.allocator.free(git_key_raw);

        // Convert to lowercase
        const git_key = try self.allocator.alloc(u8, git_key_raw.len);
        defer self.allocator.free(git_key);
        for (git_key_raw, 0..) |c, i| {
            git_key[i] = std.ascii.toLower(c);
        }

        return try r.getConfig(git_key);
    }
};

test "Resolver: Fallback chains and priority" {
    var resolver = Resolver.init(std.testing.allocator, null, "test", "default");
    defer resolver.deinit();

    try std.testing.expect((try resolver.get("password")) == null);
    try resolver.setCliArg("password", "cli_secret");

    const res = try resolver.get("password");
    try std.testing.expect(res != null);
    try std.testing.expectEqualStrings("cli_secret", res.?.value);
    try std.testing.expect(res.?.source == .cli);

    const res_def = try resolver.getStringOrDefault("missing", "def_val");
    try std.testing.expectEqualStrings("def_val", res_def.value);
    try std.testing.expect(res_def.source == .default);
}
