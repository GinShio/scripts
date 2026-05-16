const std = @import("std");
const cli = @import("../cli.zig");
const git = @import("../core/git.zig");
const crypto = @import("../core/crypto.zig");
const resolver = @import("../cli.zig");

// Config struct holding the resolved encryption parameters
const CryptConfig = struct {
    password: []const u8,
    cipher: crypto.CipherAlgorithm,
    kdf: crypto.KdfAlgorithm,
    digest: crypto.HashAlgorithm,
    iterations: ?u32,

    // Original string values for status output
    cipher_str: resolver.ResolvedValue,
    kdf_str: resolver.ResolvedValue,
    digest_str: resolver.ResolvedValue,
    iterations_str: resolver.ResolvedValue,
};

fn resolveConfig(res: *const resolver.Resolver) !?CryptConfig {
    const pwd_res = try res.get("password");
    if (pwd_res == null) return null; // Password is required

    const cipher_res = try res.getStringOrDefault("cipher", "aes-256-gcm");
    const kdf_res = try res.getStringOrDefault("kdf", "pbkdf2");
    const digest_res = try res.getStringOrDefault("digest", "sha256");
    const iters_res = try res.get("iterations");

    const cipher = if (std.mem.eql(u8, cipher_res.value, "aes-256-gcm"))
        crypto.CipherAlgorithm.aes_256_gcm
    else if (std.mem.eql(u8, cipher_res.value, "chacha20-poly1305"))
        crypto.CipherAlgorithm.chacha20_poly1305
    else {
        std.log.err("Unsupported cipher: {s}", .{cipher_res.value});
        return error.InvalidCipher;
    };

    const kdf = if (std.mem.eql(u8, kdf_res.value, "pbkdf2"))
        crypto.KdfAlgorithm.pbkdf2
    else if (std.mem.eql(u8, kdf_res.value, "argon2id"))
        crypto.KdfAlgorithm.argon2id
    else {
        std.log.err("Unsupported kdf: {s}", .{kdf_res.value});
        return error.InvalidKdf;
    };

    const digest = if (std.mem.eql(u8, digest_res.value, "sha256"))
        crypto.HashAlgorithm.sha256
    else if (std.mem.eql(u8, digest_res.value, "sha384"))
        crypto.HashAlgorithm.sha384
    else if (std.mem.eql(u8, digest_res.value, "sha512"))
        crypto.HashAlgorithm.sha512
    else if (std.mem.eql(u8, digest_res.value, "sha3256"))
        crypto.HashAlgorithm.sha3_256
    else if (std.mem.eql(u8, digest_res.value, "sha3384"))
        crypto.HashAlgorithm.sha3_384
    else if (std.mem.eql(u8, digest_res.value, "sha3512"))
        crypto.HashAlgorithm.sha3_512
    else if (std.mem.eql(u8, digest_res.value, "blake2b"))
        crypto.HashAlgorithm.blake2b
    else if (std.mem.eql(u8, digest_res.value, "blake2s"))
        crypto.HashAlgorithm.blake2s
    else {
        std.log.err("Unsupported digest: {s}", .{digest_res.value});
        return error.InvalidDigest;
    };

    var iterations: ?u32 = null;
    if (iters_res) |r_val| {
        iterations = std.fmt.parseInt(u32, r_val.value, 10) catch |err| {
            std.log.err("Invalid iterations format: {s}", .{r_val.value});
            return err;
        };
    }

    return CryptConfig{
        .password = pwd_res.?.value,
        .cipher = cipher,
        .kdf = kdf,
        .digest = digest,
        .iterations = iterations,
        .cipher_str = cipher_res,
        .kdf_str = kdf_res,
        .digest_str = digest_res,
        .iterations_str = if (iters_res) |r_val| r_val else resolver.ResolvedValue{ .value = "default", .source = .default },
    };
}

fn printCryptHelp() void {
    const stdout = std.io.getStdOut().writer();
    stdout.print(
        \\Usage: wf crypt [options] <command> [args...]
        \\
        \\Options:
        \\  -c, --context <name>   Encryption context (default: 'default')
        \\
        \\Commands:
        \\  status               Show current configuration status
        \\  clean [file]         (Internal) Encrypt data from stdin
        \\  smudge [file]        (Internal) Decrypt data from stdin
        \\  textconv <file>      (Internal) Decrypt file for diff preview
        \\
    , .{}) catch {};
}

pub fn execute(allocator: std.mem.Allocator, globals: cli.GlobalOptions, args: *std.process.ArgIterator) anyerror!void {
    _ = globals;

    var context_name: []const u8 = "default";
    var command: ?[]const u8 = null;
    var file_arg: ?[]const u8 = null;

    // Parse specific arguments
    while (args.next()) |arg| {
        if (std.mem.eql(u8, arg, "-c") or std.mem.eql(u8, arg, "--context")) {
            if (args.next()) |ctx| {
                context_name = ctx;
            } else {
                std.log.err("Option '--context' requires an argument.", .{});
                return error.MissingArgument;
            }
        } else if (std.mem.eql(u8, arg, "--help") or std.mem.eql(u8, arg, "-h")) {
            printCryptHelp();
            return;
        } else if (command == null) {
            command = arg;
        } else if (file_arg == null) {
            file_arg = arg;
        } else {
            std.log.err("Unexpected argument: {s}", .{arg});
            return error.InvalidArgument;
        }
    }

    if (command == null) {
        printCryptHelp();
        return;
    }

    // Try initializing Git Repository.
    // If not in a git repo, repo_opt will be null.
    var repo_opt: ?git.Repository = null;
    var cwd_buf: [std.fs.MAX_PATH_BYTES]u8 = undefined;
    if (std.fs.cwd().realpath(".", &cwd_buf)) |cwd| {
        // Quick check if inside a git tree. Just running `git rev-parse --show-toplevel` would be safer,
        // but for now, initializing git.Repository is fine. We will use it later for configs.
        // Let's assume we are in git repo.
        repo_opt = git.Repository.init(allocator, cwd);
    } else |_| {}

    // Initialize the Resolver
    var res_obj = resolver.Resolver.init(allocator, if (repo_opt) |*r| r else null, "transcrypt", context_name);
    defer res_obj.deinit();

    const cmd = command.?;

    if (std.mem.eql(u8, cmd, "status")) {
        try executeStatus(allocator, &res_obj, context_name);
    } else if (std.mem.eql(u8, cmd, "clean")) {
        try executeClean(allocator, &res_obj, file_arg);
    } else if (std.mem.eql(u8, cmd, "smudge")) {
        try executeSmudge(allocator, &res_obj, file_arg);
    } else if (std.mem.eql(u8, cmd, "textconv")) {
        if (file_arg == null) {
            std.log.err("textconv requires a file argument.", .{});
            return error.MissingArgument;
        }
        try executeTextconv(allocator, &res_obj, file_arg.?);
    } else {
        std.log.err("Unknown crypt command: {s}", .{cmd});
        printCryptHelp();
        return error.UnknownCommand;
    }
}

fn executeStatus(allocator: std.mem.Allocator, res: *const resolver.Resolver, context_name: []const u8) !void {
    const config_opt = try resolveConfig(res);
    const stdout = std.io.getStdOut().writer();

    try stdout.print("Status for context '{s}':\n", .{context_name});

    if (config_opt) |cfg| {
        // Find password source
        const pwd_res = (try res.get("password")).?;
        try stdout.print("  Password:   **** ({s})\n", .{@tagName(pwd_res.source)});
        try stdout.print("  Cipher:     {s} ({s})\n", .{ cfg.cipher_str.value, @tagName(cfg.cipher_str.source) });
        try stdout.print("  KDF:        {s} ({s})\n", .{ cfg.kdf_str.value, @tagName(cfg.kdf_str.source) });
        try stdout.print("  Digest:     {s} ({s})\n", .{ cfg.digest_str.value, @tagName(cfg.digest_str.source) });

        if (cfg.iterations) |iters| {
            try stdout.print("  Iterations: {d} ({s})\n", .{ iters, @tagName(cfg.iterations_str.source) });
        } else {
            const def_iters = crypto.getDefaultIterations(cfg.kdf);
            try stdout.print("  Iterations: {d} (Default for {s})\n", .{ def_iters, @tagName(cfg.kdf_str.value) });
        }
    } else {
        try stdout.print("  Password:   NOT SET\n", .{});
        try stdout.print("\nWarning: Password not found in git config or environment. Encryption/Decryption will fail.\n", .{});
        try stdout.print("Run 'git config transcrypt{s}.password <your-password>'\n", .{if (std.mem.eql(u8, context_name, "default")) "" else try std.fmt.allocPrint(allocator, ".{s}", .{context_name})});
    }

    // Check if filters are installed
    if (res.repo) |r| {
        const driver_name = if (std.mem.eql(u8, context_name, "default")) "transcrypt" else try std.fmt.allocPrint(allocator, "transcrypt-{s}", .{context_name});
        const filter_key = try std.fmt.allocPrint(allocator, "filter.{s}.clean", .{driver_name});
        if (try r.getConfig(filter_key)) |cmd_val| {
            try stdout.print("  Filters:    Installed\n", .{});
            try stdout.print("  Clean CMD:  {s}\n", .{cmd_val});
        } else {
            try stdout.print("  Filters:    Not Installed\n", .{});
            try stdout.print("\nWarning: Git filters not installed. Automatic encryption will not work.\n", .{});
            try stdout.print("Ensure you use dotfiles to manage your Git config.\n", .{});
        }
    }
}

fn executeClean(allocator: std.mem.Allocator, res: *const resolver.Resolver, file_path: ?[]const u8) !void {
    const config_opt = try resolveConfig(res);
    if (config_opt == null) {
        std.log.err("Encryption failed: Password not set.", .{});
        std.process.exit(1);
    }
    const cfg = config_opt.?;

    const stdin = std.io.getStdIn().reader();
    const stdout = std.io.getStdOut().writer();

    const data = try stdin.readAllAlloc(allocator, std.math.maxInt(usize));
    defer allocator.free(data);

    if (data.len == 0) return;

    const ctx_val = file_path orelse "";
    const options = crypto.EncryptOptions{
        .cipher = cfg.cipher,
        .kdf = cfg.kdf,
        .hash = cfg.digest,
        .iterations = cfg.iterations,
        .siv_mode = .{ .local_deterministic = .{ .context = ctx_val } },
    };

    const encrypted_b64 = try crypto.encrypt(allocator, data, cfg.password, options);
    defer allocator.free(encrypted_b64);

    try stdout.writeAll(encrypted_b64);
}

fn executeSmudge(allocator: std.mem.Allocator, res_obj: *const resolver.Resolver, file_path: ?[]const u8) !void {
    const stdin = std.io.getStdIn().reader();
    const stdout = std.io.getStdOut().writer();

    const data = try stdin.readAllAlloc(allocator, std.math.maxInt(usize));
    defer allocator.free(data);

    if (data.len == 0) return;

    if (!isEncryptedFormat(allocator, data)) {
        // Pass-through
        try stdout.writeAll(data);
        return;
    }

    const config_opt = try resolveConfig(res_obj);
    if (config_opt == null) {
        // Password not found. Graceful degradation: output raw encrypted data.
        try stdout.writeAll(data);
        return;
    }
    const cfg = config_opt.?;
    const ctx_val = file_path orelse "";

    const options = crypto.DecryptOptions{
        .cipher = cfg.cipher,
        .kdf = cfg.kdf,
        .hash = cfg.digest,
        .iterations = cfg.iterations,
        .verify_context = ctx_val,
    };

    if (crypto.decrypt(allocator, data, cfg.password, options)) |decrypted| {
        defer allocator.free(decrypted);
        try stdout.writeAll(decrypted);
    } else |err| {
        // Check Fallback
        if (std.process.getEnvVarOwned(allocator, "TRANSCRYPT_ALLOW_RAW_FALLBACK")) |fallback| {
            defer allocator.free(fallback);
            if (std.mem.eql(u8, fallback, "1") or std.ascii.eqlIgnoreCase(fallback, "true")) {
                std.log.err("Warning: Decryption failed ({s}). Outputting raw data (Fallback Mode).", .{@errorName(err)});
                try stdout.writeAll(data);
                std.process.exit(0);
            }
        } else |_| {}

        std.log.err("Decryption failed: {s}", .{@errorName(err)});
        std.process.exit(1);
    }
}

fn executeTextconv(allocator: std.mem.Allocator, res_obj: *const resolver.Resolver, file_path: []const u8) !void {
    const stdout = std.io.getStdOut().writer();

    const file = try std.fs.cwd().openFile(file_path, .{});
    defer file.close();

    const file_size = try file.getEndPos();
    const data = try file.readToEndAlloc(allocator, file_size);
    defer allocator.free(data);

    if (data.len == 0) return;

    if (!isEncryptedFormat(allocator, data)) {
        try stdout.writeAll(data);
        return;
    }

    const config_opt = try resolveConfig(res_obj);
    if (config_opt == null) {
        try stdout.writeAll(data);
        return;
    }
    const cfg = config_opt.?;

    const options = crypto.DecryptOptions{
        .cipher = cfg.cipher,
        .kdf = cfg.kdf,
        .hash = cfg.digest,
        .iterations = cfg.iterations,
        .verify_context = file_path,
    };

    if (crypto.decrypt(allocator, data, cfg.password, options)) |decrypted| {
        defer allocator.free(decrypted);
        try stdout.writeAll(decrypted);
    } else |err| {
        if (std.process.getEnvVarOwned(allocator, "TRANSCRYPT_ALLOW_RAW_FALLBACK")) |fallback| {
            defer allocator.free(fallback);
            if (std.mem.eql(u8, fallback, "1") or std.ascii.eqlIgnoreCase(fallback, "true")) {
                std.log.err("Warning: Textconv failed ({s}). Outputting raw data (Fallback Mode).", .{@errorName(err)});
                try stdout.writeAll(data);
                std.process.exit(0);
            }
        } else |_| {}

        std.log.err("Textconv failed: {s}", .{@errorName(err)});
        std.process.exit(1);
    }
}

/// Simple heuristic to check if the data might be encrypted.
fn isEncryptedFormat(allocator: std.mem.Allocator, data: []const u8) bool {
    const b64_decoder = std.base64.standard.Decoder;
    const estimated_size = b64_decoder.calcSizeForSlice(data) catch return false;

    // We only need to check the first 8 bytes (Salted__)
    if (estimated_size < crypto.SALT_HEADER.len) return false;

    // Decode just enough to check header

    // Find the prefix length that gives us at least 16 bytes decoded
    // Since 4 base64 chars = 3 bytes, to get 16 bytes we need ceil(16 * 4/3) = 22 base64 chars
    const prefix_len = if (data.len < 24) data.len else 24;

    const temp_buf = allocator.alloc(u8, b64_decoder.calcSizeForSlice(data[0..prefix_len]) catch return false) catch return false;
    defer allocator.free(temp_buf);

    b64_decoder.decode(temp_buf, data[0..prefix_len]) catch return false;

    return std.mem.startsWith(u8, temp_buf, crypto.SALT_HEADER);
}

// -----------------------------------------------------------------------------
// Unit Tests
// -----------------------------------------------------------------------------

test "Crypt - isEncryptedFormat" {
    // Generate some valid base64 encrypted mock
    const raw = "Salted__12345678IVed__123456789012abcdefgh";
    var b64_buf: [200]u8 = undefined;
    const b64 = std.base64.standard.Encoder.encode(&b64_buf, raw);

    try std.testing.expect(isEncryptedFormat(std.testing.allocator, b64));

    // Plain text
    try std.testing.expect(!isEncryptedFormat(std.testing.allocator, "hello world this is definitely not encrypted"));

    // Invalid base64
    try std.testing.expect(!isEncryptedFormat(std.testing.allocator, "Salted__but no base64?@#$!"));
}
