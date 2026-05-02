const std = @import("std");
const process = @import("process.zig");
const log = @import("log.zig");

/// A high-level, pure CLI-based Git API.
///
/// Design Philosophy:
/// Unlike the previous Python implementation which mixed `libgit2` (for reads)
/// and `git` CLI (for writes), this Zig implementation relies entirely on the
/// `git` CLI via our robust `process.Command` orchestrator.
///
/// Justification for pure CLI:
/// 1. Zero Dependencies: Eliminates the need to link against `libgit2`, making
///    the binary fully standalone and drastically simplifying the build system.
/// 2. 100% Compatibility: `libgit2` often struggles with complex `.gitconfig`
///    includes, credential helpers, and SSH agents. The CLI always works exactly
///    as the user expects.
/// 3. Performance: With Zig's fast process spawning and our efficient `process.zig`,
///    the overhead of calling `git` CLI is negligible for workflow orchestration tasks.
pub const Repository = struct {
    allocator: std.mem.Allocator,
    path: []const u8,

    pub fn init(allocator: std.mem.Allocator, path: []const u8) Repository {
        return .{
            .allocator = allocator,
            .path = path,
        };
    }

    /// Creates a base Git command pre-configured with the repository path.
    /// The caller must call `deinit` on the returned Command.
    pub fn cmd(self: *const Repository) process.Command {
        var c = process.Command.init(self.allocator, "git");
        _ = c.setCwd(self.path);
        return c;
    }

    // -------------------------------------------------------------------------
    // Configuration
    // -------------------------------------------------------------------------

    /// Reads a git configuration value.
    /// Returns null if the key doesn't exist.
    /// The caller owns the returned string and must free it.
    pub fn getConfig(self: *const Repository, key: []const u8) !?[]const u8 {
        var c = self.cmd();
        defer c.deinit();

        // We force run because reading config is a safe, read-only operation
        // that is often required for control flow even during dry-runs.
        _ = c.arg("config").arg("--get").arg(key).forceRun();

        const res = try c.exec();
        defer res.deinit();

        if (res.isSuccess() and res.stdout.len > 0) {
            // Trim trailing newline
            const trimmed = std.mem.trimRight(u8, res.stdout, "\r\n");
            return try self.allocator.dupe(u8, trimmed);
        }
        return null;
    }

    /// Sets a git configuration value.
    pub fn setConfig(self: *const Repository, key: []const u8, value: []const u8) !void {
        var c = self.cmd();
        defer c.deinit();
        const res = try c.arg("config").arg(key).arg(value).execCheck();
        res.deinit();
    }

    // -------------------------------------------------------------------------
    // Inspection & Status
    // -------------------------------------------------------------------------

    /// Resolves a revision specifier (e.g., "HEAD", "main") to a full commit hash.
    /// Returns null if the specifier is invalid.
    /// The caller owns the returned string.
    pub fn resolveCommit(self: *const Repository, spec: []const u8) !?[]const u8 {
        var c = self.cmd();
        defer c.deinit();

        _ = c.arg("rev-parse").arg("--verify").arg(spec).forceRun();
        const res = try c.exec();
        defer res.deinit();

        if (res.isSuccess() and res.stdout.len > 0) {
            const trimmed = std.mem.trimRight(u8, res.stdout, "\r\n");
            return try self.allocator.dupe(u8, trimmed);
        }
        return null;
    }

    /// Gets the name of the current branch.
    /// Returns null if in a detached HEAD state.
    /// The caller owns the returned string.
    pub fn getHeadBranch(self: *const Repository) !?[]const u8 {
        var c = self.cmd();
        defer c.deinit();

        _ = c.arg("symbolic-ref").arg("--short").arg("HEAD").forceRun();
        const res = try c.exec();
        defer res.deinit();

        if (res.isSuccess() and res.stdout.len > 0) {
            const trimmed = std.mem.trimRight(u8, res.stdout, "\r\n");
            return try self.allocator.dupe(u8, trimmed);
        }
        return null;
    }

    /// Checks if the working directory is dirty (has uncommitted changes).
    pub fn isDirty(self: *const Repository, include_untracked: bool) !bool {
        var c = self.cmd();
        defer c.deinit();

        _ = c.arg("status").arg("--porcelain").forceRun();
        if (!include_untracked) {
            _ = c.arg("-uno");
        }

        const res = try c.execCheck();
        defer res.deinit();

        // If there is any output, the repository is dirty
        return res.stdout.len > 0;
    }

    /// Gets the default branch for a given remote (e.g., "main" or "master").
    /// The caller owns the returned string.
    pub fn getDefaultBranch(self: *const Repository, remote_name: []const u8) !?[]const u8 {
        var c = self.cmd();
        defer c.deinit();

        // Try to read refs/remotes/<remote>/HEAD
        const ref = try std.fmt.allocPrint(self.allocator, "refs/remotes/{s}/HEAD", .{remote_name});
        defer self.allocator.free(ref);

        _ = c.arg("symbolic-ref").arg("--short").arg(ref).forceRun();
        const res = try c.exec();
        defer res.deinit();

        if (res.isSuccess() and res.stdout.len > 0) {
            const trimmed = std.mem.trimRight(u8, res.stdout, "\r\n");
            // Output is typically "origin/main", we just want "main"
            const prefix = try std.fmt.allocPrint(self.allocator, "{s}/", .{remote_name});
            defer self.allocator.free(prefix);

            if (std.mem.startsWith(u8, trimmed, prefix)) {
                return try self.allocator.dupe(u8, trimmed[prefix.len..]);
            }
            return try self.allocator.dupe(u8, trimmed);
        }

        // Fallback: check if main or master exists locally
        if (try self.resolveCommit("main")) |hash| {
            self.allocator.free(hash);
            return try self.allocator.dupe(u8, "main");
        }
        if (try self.resolveCommit("master")) |hash| {
            self.allocator.free(hash);
            return try self.allocator.dupe(u8, "master");
        }

        return null;
    }

    // -------------------------------------------------------------------------
    // Remote Management
    // -------------------------------------------------------------------------

    /// Gets the URL for a specific remote.
    /// The caller owns the returned string.
    pub fn getRemoteUrl(self: *const Repository, remote_name: []const u8) !?[]const u8 {
        var c = self.cmd();
        defer c.deinit();

        _ = c.arg("remote").arg("get-url").arg(remote_name).forceRun();
        const res = try c.exec();
        defer res.deinit();

        if (res.isSuccess() and res.stdout.len > 0) {
            const trimmed = std.mem.trimRight(u8, res.stdout, "\r\n");
            return try self.allocator.dupe(u8, trimmed);
        }
        return null;
    }

    /// Gets all URLs for a specific remote.
    /// If `push` is true, returns push URLs instead of fetch URLs.
    /// The caller owns the returned slice and its string elements.
    pub fn getRemoteUrls(self: *const Repository, remote_name: []const u8, push: bool) ![][]const u8 {
        var c = self.cmd();
        defer c.deinit();

        _ = c.arg("remote").arg("get-url").arg("--all");
        if (push) _ = c.arg("--push");
        _ = c.arg(remote_name).forceRun();

        const res = try c.exec();
        defer res.deinit();

        var urls = std.ArrayList([]const u8).init(self.allocator);
        errdefer {
            for (urls.items) |item| self.allocator.free(item);
            urls.deinit();
        }

        if (res.isSuccess() and res.stdout.len > 0) {
            var it = std.mem.splitScalar(u8, res.stdout, '\n');
            while (it.next()) |line| {
                if (line.len == 0) continue;
                try urls.append(try self.allocator.dupe(u8, line));
            }
        }
        return try urls.toOwnedSlice();
    }

    /// Lists all remote names.
    /// The caller owns the returned slice and its string elements.
    pub fn listRemotes(self: *const Repository) ![][]const u8 {
        var c = self.cmd();
        defer c.deinit();

        _ = c.arg("remote").forceRun();
        const res = try c.exec();
        defer res.deinit();

        var remotes = std.ArrayList([]const u8).init(self.allocator);
        errdefer {
            for (remotes.items) |item| self.allocator.free(item);
            remotes.deinit();
        }

        if (res.isSuccess() and res.stdout.len > 0) {
            var it = std.mem.splitScalar(u8, res.stdout, '\n');
            while (it.next()) |line| {
                if (line.len == 0) continue;
                try remotes.append(try self.allocator.dupe(u8, line));
            }
        }
        return try remotes.toOwnedSlice();
    }

    /// Adds a new remote.
    pub fn addRemote(self: *const Repository, remote_name: []const u8, url: []const u8) !void {
        var c = self.cmd();
        defer c.deinit();
        const res = try c.arg("remote").arg("add").arg(remote_name).arg(url).execCheck();
        res.deinit();
    }

    /// Renames an existing remote.
    pub fn renameRemote(self: *const Repository, old_name: []const u8, new_name: []const u8) !void {
        var c = self.cmd();
        defer c.deinit();
        const res = try c.arg("remote").arg("rename").arg(old_name).arg(new_name).execCheck();
        res.deinit();
    }

    /// Sets the URL for an existing remote.
    /// If `push` is true, sets the push URL instead of the fetch URL.
    /// If `add` is true, adds an additional URL (useful for multiple push URLs).
    pub fn setRemoteUrl(self: *const Repository, remote_name: []const u8, url: []const u8, push: bool, add: bool) !void {
        var c = self.cmd();
        defer c.deinit();

        _ = c.arg("remote").arg("set-url");
        if (push) _ = c.arg("--push");
        if (add) _ = c.arg("--add");
        const res = try c.arg(remote_name).arg(url).execCheck();
        res.deinit();
    }
};

// -----------------------------------------------------------------------------
// Unit Tests
// -----------------------------------------------------------------------------

fn setupTestRepo(allocator: std.mem.Allocator, path: []const u8) !void {
    std.fs.cwd().makeDir(path) catch |err| {
        if (err != error.PathAlreadyExists) return err;
    };

    var init_cmd = process.Command.init(allocator, "git");
    defer init_cmd.deinit();
    const res_init = try init_cmd.arg("init").setCwd(path).execCheck();
    res_init.deinit();

    var cfg1 = process.Command.init(allocator, "git");
    defer cfg1.deinit();
    const res_cfg1 = try cfg1.arg("config").arg("user.name").arg("Test User").setCwd(path).execCheck();
    res_cfg1.deinit();

    var cfg2 = process.Command.init(allocator, "git");
    defer cfg2.deinit();
    const res_cfg2 = try cfg2.arg("config").arg("user.email").arg("test@example.com").setCwd(path).execCheck();
    res_cfg2.deinit();
}

test "Git API - Config operations" {
    const tmp_path = "test_repo_config";
    try setupTestRepo(std.testing.allocator, tmp_path);
    defer std.fs.cwd().deleteTree(tmp_path) catch {};

    const repo = Repository.init(std.testing.allocator, tmp_path);

    // Set config
    try repo.setConfig("workflow.test.key", "hello_world");

    // Get config
    const val = try repo.getConfig("workflow.test.key");
    try std.testing.expect(val != null);
    defer std.testing.allocator.free(val.?);
    try std.testing.expectEqualStrings("hello_world", val.?);

    // Get non-existent config
    const missing = try repo.getConfig("workflow.test.missing");
    try std.testing.expect(missing == null);
}

test "Git API - Status and Commits" {
    const tmp_path = "test_repo_status";
    try setupTestRepo(std.testing.allocator, tmp_path);
    defer std.fs.cwd().deleteTree(tmp_path) catch {};

    const repo = Repository.init(std.testing.allocator, tmp_path);

    // Initially clean
    try std.testing.expect(!(try repo.isDirty(true)));

    // Create a file
    const file = try std.fs.cwd().createFile("test_repo_status/test.txt", .{});
    try file.writeAll("hello");
    file.close();

    // Now it's dirty (untracked)
    try std.testing.expect(try repo.isDirty(true));
    // If we don't include untracked, it shouldn't be dirty
    try std.testing.expect(!(try repo.isDirty(false)));

    // Add and commit
    var add_cmd = process.Command.init(std.testing.allocator, "git");
    defer add_cmd.deinit();
    const res_add = try add_cmd.arg("add").arg("test.txt").setCwd(tmp_path).execCheck();
    res_add.deinit();

    var commit_cmd = process.Command.init(std.testing.allocator, "git");
    defer commit_cmd.deinit();
    const res_commit = try commit_cmd.arg("commit").arg("-m").arg("Initial commit").setCwd(tmp_path).execCheck();
    res_commit.deinit();

    // Clean again
    try std.testing.expect(!(try repo.isDirty(true)));

    // Check branch
    const branch = try repo.getHeadBranch();
    try std.testing.expect(branch != null);
    defer std.testing.allocator.free(branch.?);
    try std.testing.expect(branch.?.len > 0);

    // Resolve HEAD
    const head_hash = try repo.resolveCommit("HEAD");
    try std.testing.expect(head_hash != null);
    defer std.testing.allocator.free(head_hash.?);
    try std.testing.expectEqual(@as(usize, 40), head_hash.?.len);
}

test "Git API - Edge cases for Commits and Branches" {
    const tmp_path = "test_repo_edge_cases";
    try setupTestRepo(std.testing.allocator, tmp_path);
    defer std.fs.cwd().deleteTree(tmp_path) catch {};

    const repo = Repository.init(std.testing.allocator, tmp_path);

    // 1. Unborn branch (empty repo)
    // resolveCommit("HEAD") should return null
    const hash_unborn = try repo.resolveCommit("HEAD");
    try std.testing.expect(hash_unborn == null);

    // 2. Invalid commit specifier
    const hash_invalid = try repo.resolveCommit("this_does_not_exist");
    try std.testing.expect(hash_invalid == null);

    // Create a commit to test detached HEAD
    const file = try std.fs.cwd().createFile("test_repo_edge_cases/test.txt", .{});
    try file.writeAll("hello");
    file.close();

    var add_cmd = process.Command.init(std.testing.allocator, "git");
    defer add_cmd.deinit();
    const res_add = try add_cmd.arg("add").arg("test.txt").setCwd(tmp_path).execCheck();
    res_add.deinit();

    var commit_cmd = process.Command.init(std.testing.allocator, "git");
    defer commit_cmd.deinit();
    const res_commit = try commit_cmd.arg("commit").arg("-m").arg("Initial").setCwd(tmp_path).execCheck();
    res_commit.deinit();

    const head_hash = try repo.resolveCommit("HEAD");
    try std.testing.expect(head_hash != null);
    defer std.testing.allocator.free(head_hash.?);

    // 3. Detached HEAD
    var checkout_cmd = process.Command.init(std.testing.allocator, "git");
    defer checkout_cmd.deinit();
    const res_checkout = try checkout_cmd.arg("checkout").arg(head_hash.?).setCwd(tmp_path).execCheck();
    res_checkout.deinit();

    const detached_branch = try repo.getHeadBranch();
    try std.testing.expect(detached_branch == null);
}

test "Git API - isDirty with tracked files" {
    const tmp_path = "test_repo_dirty_tracked";
    try setupTestRepo(std.testing.allocator, tmp_path);
    defer std.fs.cwd().deleteTree(tmp_path) catch {};

    const repo = Repository.init(std.testing.allocator, tmp_path);

    // Create and commit
    const file = try std.fs.cwd().createFile("test_repo_dirty_tracked/test.txt", .{});
    try file.writeAll("hello");
    file.close();

    var add_cmd = process.Command.init(std.testing.allocator, "git");
    defer add_cmd.deinit();
    const res_add = try add_cmd.arg("add").arg("test.txt").setCwd(tmp_path).execCheck();
    res_add.deinit();

    var commit_cmd = process.Command.init(std.testing.allocator, "git");
    defer commit_cmd.deinit();
    const res_commit = try commit_cmd.arg("commit").arg("-m").arg("Initial").setCwd(tmp_path).execCheck();
    res_commit.deinit();

    // Modify tracked file
    const file_mod = try std.fs.cwd().createFile("test_repo_dirty_tracked/test.txt", .{});
    try file_mod.writeAll("world");
    file_mod.close();

    // Should be dirty even if include_untracked is false
    try std.testing.expect(try repo.isDirty(false));
    try std.testing.expect(try repo.isDirty(true));
}

test "Git API - Advanced Remote operations" {
    const tmp_path = "test_repo_adv_remote";
    try setupTestRepo(std.testing.allocator, tmp_path);
    defer std.fs.cwd().deleteTree(tmp_path) catch {};

    const repo = Repository.init(std.testing.allocator, tmp_path);

    try repo.addRemote("origin", "https://example.com/1.git");
    try repo.setRemoteUrl("origin", "https://example.com/2.git", true, true);

    const remotes = try repo.listRemotes();
    defer {
        for (remotes) |r| std.testing.allocator.free(r);
        std.testing.allocator.free(remotes);
    }
    try std.testing.expectEqual(@as(usize, 1), remotes.len);
    try std.testing.expectEqualStrings("origin", remotes[0]);

    const push_urls = try repo.getRemoteUrls("origin", true);
    defer {
        for (push_urls) |u| std.testing.allocator.free(u);
        std.testing.allocator.free(push_urls);
    }
    try std.testing.expect(push_urls.len > 0);

    try repo.renameRemote("origin", "upstream");
    const remotes2 = try repo.listRemotes();
    defer {
        for (remotes2) |r| std.testing.allocator.free(r);
        std.testing.allocator.free(remotes2);
    }
    try std.testing.expectEqualStrings("upstream", remotes2[0]);

    // Test getDefaultBranch
    // Since we didn't fetch, refs/remotes/upstream/HEAD won't exist.
    // But it should fallback to local main/master. We don't have those yet, so it should be null.
    const def_branch = try repo.getDefaultBranch("upstream");
    try std.testing.expect(def_branch == null);
}
