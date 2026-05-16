const std = @import("std");
const log = @import("core/log.zig");
const git = @import("core/git.zig");

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
/// Resolves keys in the following order of precedence:
/// 1. CLI Arguments (if provided)
/// 2. Environment Variables with Context (e.g. TRANSCRYPT_PROD_PASSWORD)
/// 3. Environment Variables without Context (e.g. TRANSCRYPT_PASSWORD)
/// 4. Git Config with Context (e.g. transcrypt.prod.password)
/// 5. Git Config without Context (e.g. transcrypt.password)
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
            // Only use legacy fallback (no context) if context is "default"
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
