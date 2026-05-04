const std = @import("std");

/// Supported cipher algorithms.
/// Only modern Authenticated Encryption with Associated Data (AEAD) ciphers are supported.
pub const CipherAlgorithm = enum {
    aes_256_gcm,
    chacha20_poly1305,
};

/// Supported Key Derivation Functions (KDF).
pub const KdfAlgorithm = enum {
    pbkdf2,
    argon2id,
};

/// Supported Cryptographic Hash Functions.
/// Kept SHA-2 family, SHA-3 family, and BLAKE2b/BLAKE3.
/// (Note: Zig std library blake3 might not be directly in std.crypto.hash, but blake2b is.
/// We'll map blake3 to blake2b if blake3 isn't available, or wait for std support).
pub const HashAlgorithm = enum {
    sha256,
    sha384,
    sha512,
    sha3_256,
    sha3_384,
    sha3_512,
    blake2b,
    blake2s,
};

/// Controls how Salt and IV are generated.
pub const SivMode = union(enum) {
    /// Local Mode: Fully compatible with the Python implementation.
    /// Both Salt and IV are deterministically derived from:
    /// Hash( Algo_Params || Password || Context || Plaintext )
    local_deterministic: struct {
        context: []const u8,
    },
    /// Global Mode: High-performance deterministic mode.
    /// The Salt is globally fixed (e.g., derived once from Context or Git Config).
    /// Only the IV is derived from the Plaintext.
    global_deterministic: struct {
        salt: [8]u8,
        context: []const u8,
    },
    /// Random Mode: Standard randomized encryption.
    /// Salt and IV are generated using a Cryptographically Secure PRNG.
    random,
};

pub const EncryptOptions = struct {
    cipher: CipherAlgorithm = .aes_256_gcm,
    kdf: KdfAlgorithm = .pbkdf2,
    hash: HashAlgorithm = .sha256,
    iterations: u32 = 99989, // Default to Python's PBKDF2 default
    siv_mode: SivMode,
};

pub const DecryptOptions = struct {
    cipher: CipherAlgorithm = .aes_256_gcm,
    kdf: KdfAlgorithm = .pbkdf2,
    hash: HashAlgorithm = .sha256,
    iterations: u32 = 99989,
    /// If provided, ensures the decrypted content matches this context
    verify_context: ?[]const u8 = null,
};

// OpenSSL-compatible format constants used by the Python implementation
pub const SALT_HEADER = "Salted__";
pub const IV_HEADER = "IVed__";
pub const SALT_SIZE = 8;

/// Returns the byte length of the Nonce/IV for a given cipher
pub fn getNonceSize(cipher: CipherAlgorithm) usize {
    return switch (cipher) {
        .aes_256_gcm => 12,
        .chacha20_poly1305 => 12,
    };
}

/// Returns the Key length for a given cipher
pub fn getKeySize(cipher: CipherAlgorithm) usize {
    return switch (cipher) {
        .aes_256_gcm => 32, // AES-256
        .chacha20_poly1305 => 32,
    };
}

/// Helper to serialize algorithm name matching Python version
fn getCipherName(cipher: CipherAlgorithm) []const u8 {
    return switch (cipher) {
        .aes_256_gcm => "aes-256-gcm",
        .chacha20_poly1305 => "chacha20-poly1305",
    };
}

fn getHashName(hash: HashAlgorithm) []const u8 {
    return switch (hash) {
        .sha256 => "sha256",
        .sha384 => "sha384",
        .sha512 => "sha512",
        .sha3_256 => "sha3256",
        .sha3_384 => "sha3384",
        .sha3_512 => "sha3512",
        .blake2b => "blake2b",
        .blake2s => "blake2s",
    };
}

fn getKdfName(kdf: KdfAlgorithm) []const u8 {
    return switch (kdf) {
        .pbkdf2 => "pbkdf2",
        .argon2id => "argon2id",
    };
}

/// Compute deterministic Salt and IV using S2V-like construction.
/// Hash(Algo_Params || SEP || Len(Pwd) || Pwd || SEP || Len(Ctx) || Ctx || SEP || Data)
/// This exactly matches Python's `_compute_siv_params`.
pub fn computeSivParams(
    allocator: std.mem.Allocator,
    password: []const u8,
    data: []const u8,
    context: []const u8,
    options: EncryptOptions,
    out_salt: *[SALT_SIZE]u8,
    out_iv: []u8,
) !void {
    // 1. Algo Params (Length prefixed)
    // Format: digest_name:cipher_name:iterations:kdf_name
    const algo_params = try std.fmt.allocPrint(allocator, "{s}:{s}:{d}:{s}", .{
        getHashName(options.hash),
        getCipherName(options.cipher),
        options.iterations,
        getKdfName(options.kdf),
    });
    defer allocator.free(algo_params);

    // We will hash everything iteratively
    // Map HashAlgorithm to std.crypto.hash types
    switch (options.hash) {
        .sha256 => try doComputeSivParams(std.crypto.hash.sha2.Sha256, algo_params, password, context, data, out_salt, out_iv),
        .sha384 => try doComputeSivParams(std.crypto.hash.sha2.Sha384, algo_params, password, context, data, out_salt, out_iv),
        .sha512 => try doComputeSivParams(std.crypto.hash.sha2.Sha512, algo_params, password, context, data, out_salt, out_iv),
        .sha3_256 => try doComputeSivParams(std.crypto.hash.sha3.Sha3_256, algo_params, password, context, data, out_salt, out_iv),
        .sha3_384 => try doComputeSivParams(std.crypto.hash.sha3.Sha3_384, algo_params, password, context, data, out_salt, out_iv),
        .sha3_512 => try doComputeSivParams(std.crypto.hash.sha3.Sha3_512, algo_params, password, context, data, out_salt, out_iv),
        .blake2b => try doComputeSivParams(std.crypto.hash.blake2.Blake2b384, algo_params, password, context, data, out_salt, out_iv),
        .blake2s => try doComputeSivParams(std.crypto.hash.blake2.Blake2s256, algo_params, password, context, data, out_salt, out_iv),
    }
}

// Inner helper to avoid duplicating the update logic for every hash type
fn doComputeSivParams(
    comptime Hasher: type,
    algo_params: []const u8,
    password: []const u8,
    context: []const u8,
    data: []const u8,
    out_salt: *[SALT_SIZE]u8,
    out_iv: []u8,
) !void {
    var h = Hasher.init(.{});
    const sep = "\x00";

    // 1. Algo Params
    var len_buf: [4]u8 = undefined;
    std.mem.writeInt(u32, &len_buf, @intCast(algo_params.len), .big);
    h.update(&len_buf);
    h.update(algo_params);
    h.update(sep);

    // 2. Length prefix password
    std.mem.writeInt(u32, &len_buf, @intCast(password.len), .big);
    h.update(&len_buf);
    h.update(password);
    h.update(sep);

    // 3. Length prefix context
    std.mem.writeInt(u32, &len_buf, @intCast(context.len), .big);
    h.update(&len_buf);
    h.update(context);
    h.update(sep);

    // 4. Data
    h.update(data);

    var digest_val: [Hasher.digest_length]u8 = undefined;
    h.final(&digest_val);

    const required_len = SALT_SIZE + out_iv.len;
    if (digest_val.len < required_len) {
        return error.DigestOutputTooShort;
    }

    // Python does: iv = digest_val[:iv_len], salt = digest_val[iv_len : iv_len+SALT_SIZE]
    @memcpy(out_iv, digest_val[0..out_iv.len]);
    @memcpy(out_salt, digest_val[out_iv.len .. out_iv.len + SALT_SIZE]);
}

// --- Key Derivation ---

pub fn deriveKey(
    allocator: std.mem.Allocator,
    password: []const u8,
    salt: []const u8,
    key_length: usize,
    options: EncryptOptions,
) ![]u8 {
    const key = try allocator.alloc(u8, key_length);
    errdefer allocator.free(key);

    switch (options.kdf) {
        .pbkdf2 => {
            switch (options.hash) {
                .sha256 => try std.crypto.pwhash.pbkdf2(key, password, salt, options.iterations, std.crypto.auth.hmac.sha2.HmacSha256),
                .sha384 => try std.crypto.pwhash.pbkdf2(key, password, salt, options.iterations, std.crypto.auth.hmac.sha2.HmacSha384),
                .sha512 => try std.crypto.pwhash.pbkdf2(key, password, salt, options.iterations, std.crypto.auth.hmac.sha2.HmacSha512),
                // PBKDF2 requires an HMAC type, but zig std doesn't expose HMAC for SHA3 or Blake2 by default in the same namespace.
                // We'll use standard HMAC builder for them.
                .sha3_256 => try std.crypto.pwhash.pbkdf2(key, password, salt, options.iterations, std.crypto.auth.hmac.Hmac(std.crypto.hash.sha3.Sha3_256)),
                .sha3_384 => try std.crypto.pwhash.pbkdf2(key, password, salt, options.iterations, std.crypto.auth.hmac.Hmac(std.crypto.hash.sha3.Sha3_384)),
                .sha3_512 => try std.crypto.pwhash.pbkdf2(key, password, salt, options.iterations, std.crypto.auth.hmac.Hmac(std.crypto.hash.sha3.Sha3_512)),
                .blake2b => try std.crypto.pwhash.pbkdf2(key, password, salt, options.iterations, std.crypto.auth.hmac.Hmac(std.crypto.hash.blake2.Blake2b384)),
                .blake2s => try std.crypto.pwhash.pbkdf2(key, password, salt, options.iterations, std.crypto.auth.hmac.Hmac(std.crypto.hash.blake2.Blake2s256)),
            }
        },
        .argon2id => {
            // Map Python's iterations logic. If it is DEFAULT_ITERATIONS, use a sane Argon2 value.
            // We use std.crypto.pwhash.argon2. Argon2 requires Memory/Lanes.
            // In python, ARGON2_DEFAULT_ITERATIONS = 4, MEMORY = 131072, LANES = 2
            const t_cost = if (options.iterations == 99989) 4 else options.iterations;
            const m_cost = 131072;

            // Standard zig argon2 has hardcoded lanes? Actually std.crypto.pwhash.argon2 handles it via options
            try std.crypto.pwhash.argon2.kdf(
                allocator,
                key,
                password,
                salt,
                .{ .t = t_cost, .m = m_cost, .p = 2 },
                .argon2id,
            );
        },
    }

    return key;
}

// --- Encrypt / Decrypt ---

pub fn encrypt(
    allocator: std.mem.Allocator,
    plaintext: []const u8,
    password: []const u8,
    options: EncryptOptions,
) ![]u8 {
    var salt: [SALT_SIZE]u8 = undefined;
    var iv = try allocator.alloc(u8, getNonceSize(options.cipher));
    defer allocator.free(iv);

    var aad: []const u8 = "";

    // 1. Determine Salt, IV and AAD based on SivMode
    switch (options.siv_mode) {
        .local_deterministic => |ld| {
            try computeSivParams(allocator, password, plaintext, ld.context, options, &salt, iv);
            aad = ld.context;
        },
        .global_deterministic => |gd| {
            @memcpy(&salt, &gd.salt);
            // For global mode, we still need to make IV deterministic based on plaintext and context.
            // Python doesn't have "global_deterministic" natively, but conceptually we can hash to get IV
            var tmp_salt: [SALT_SIZE]u8 = undefined;
            // computeSivParams generates both. We just discard the generated salt and use the global one.
            try computeSivParams(allocator, password, plaintext, gd.context, options, &tmp_salt, iv);
            aad = gd.context;
        },
        .random => {
            std.crypto.random.bytes(&salt);
            std.crypto.random.bytes(iv);
        },
    }

    // 2. Derive Key
    const key = try deriveKey(allocator, password, &salt, getKeySize(options.cipher), options);
    defer allocator.free(key);

    // 3. Encrypt Plaintext
    // The MAC Tag is usually appended to the ciphertext
    // Length required: SALT_HEADER + SALT_SIZE + IV_HEADER + iv.len + plaintext.len + tag.len
    const tag_size: usize = 16;
    const packet_len = SALT_HEADER.len + SALT_SIZE + IV_HEADER.len + iv.len + plaintext.len + tag_size;
    var packet = try allocator.alloc(u8, packet_len);
    errdefer allocator.free(packet);

    // Copy headers and parameters
    var offset: usize = 0;
    @memcpy(packet[offset .. offset + SALT_HEADER.len], SALT_HEADER);
    offset += SALT_HEADER.len;
    @memcpy(packet[offset .. offset + SALT_SIZE], &salt);
    offset += SALT_SIZE;

    @memcpy(packet[offset .. offset + IV_HEADER.len], IV_HEADER);
    offset += IV_HEADER.len;
    @memcpy(packet[offset .. offset + iv.len], iv);
    offset += iv.len;

    const ciphertext_and_tag = packet[offset..];

    // Encrypt depending on cipher
    switch (options.cipher) {
        .chacha20_poly1305 => {
            const chacha = std.crypto.aead.chacha_poly.ChaCha20Poly1305;
            var out_tag: [16]u8 = undefined;
            var key_arr: [32]u8 = undefined;
            @memcpy(&key_arr, key[0..32]);
            var nonce_arr: [12]u8 = undefined;
            @memcpy(&nonce_arr, iv[0..12]);

            // encrypt(c, m, ad, npub, k)
            chacha.encrypt(ciphertext_and_tag[0..plaintext.len], &out_tag, plaintext, aad, nonce_arr, key_arr);
            @memcpy(ciphertext_and_tag[plaintext.len .. plaintext.len + 16], &out_tag);
        },
        .aes_256_gcm => {
            const gcm = std.crypto.aead.aes_gcm.Aes256Gcm;
            var out_tag: [16]u8 = undefined;
            var key_arr: [32]u8 = undefined;
            @memcpy(&key_arr, key[0..32]);
            var nonce_arr: [12]u8 = undefined;
            @memcpy(&nonce_arr, iv[0..12]);

            gcm.encrypt(ciphertext_and_tag[0..plaintext.len], &out_tag, plaintext, aad, nonce_arr, key_arr);
            @memcpy(ciphertext_and_tag[plaintext.len .. plaintext.len + 16], &out_tag);
        },
    }

    // 4. Base64 Encode
    const b64_encoder = std.base64.standard.Encoder;
    const b64_len = b64_encoder.calcSize(packet.len);
    const b64_out = try allocator.alloc(u8, b64_len);
    _ = b64_encoder.encode(b64_out, packet);

    // We can free the raw packet now
    allocator.free(packet);

    return b64_out;
}

pub fn decrypt(
    allocator: std.mem.Allocator,
    ciphertext_b64: []const u8,
    password: []const u8,
    options: DecryptOptions,
) ![]u8 {
    const b64_decoder = std.base64.standard.Decoder;
    const packet_len = try b64_decoder.calcSizeForSlice(ciphertext_b64);
    var packet = try allocator.alloc(u8, packet_len);
    defer allocator.free(packet);

    try b64_decoder.decode(packet, ciphertext_b64);

    var offset: usize = 0;

    // Validate and Extract Salt
    if (!std.mem.startsWith(u8, packet[offset..], SALT_HEADER)) return error.MissingSaltHeader;
    offset += SALT_HEADER.len;

    var salt: [SALT_SIZE]u8 = undefined;
    @memcpy(&salt, packet[offset .. offset + SALT_SIZE]);
    offset += SALT_SIZE;

    // Validate and Extract IV
    if (!std.mem.startsWith(u8, packet[offset..], IV_HEADER)) return error.MissingIVHeader;
    offset += IV_HEADER.len;

    const iv_len = getNonceSize(options.cipher);
    var iv = try allocator.alloc(u8, iv_len);
    defer allocator.free(iv);
    @memcpy(iv, packet[offset .. offset + iv_len]);
    offset += iv_len;

    // Remaining is Ciphertext + Tag (16 bytes)
    const encrypted_data = packet[offset..];
    if (encrypted_data.len < 16) return error.CiphertextTooShort;

    const ciphertext_len = encrypted_data.len - 16;
    const ciphertext = encrypted_data[0..ciphertext_len];
    const tag = encrypted_data[ciphertext_len..];

    // Derive Key
    const enc_options = EncryptOptions{
        .cipher = options.cipher,
        .kdf = options.kdf,
        .hash = options.hash,
        .iterations = options.iterations,
        .siv_mode = .random, // Dummy value, we don't use siv_mode in decrypt directly for key generation
    };
    const key = try deriveKey(allocator, password, &salt, getKeySize(options.cipher), enc_options);
    defer allocator.free(key);

    const plaintext = try allocator.alloc(u8, ciphertext_len);
    errdefer allocator.free(plaintext);

    const aad: []const u8 = if (options.verify_context) |ctx| ctx else "";

    // Decrypt
    switch (options.cipher) {
        .chacha20_poly1305 => {
            const chacha = std.crypto.aead.chacha_poly.ChaCha20Poly1305;
            var tag_arr: [16]u8 = undefined;
            @memcpy(&tag_arr, tag[0..16]);
            var key_arr: [32]u8 = undefined;
            @memcpy(&key_arr, key[0..32]);
            var nonce_arr: [12]u8 = undefined;
            @memcpy(&nonce_arr, iv[0..12]);

            chacha.decrypt(plaintext, ciphertext, tag_arr, aad, nonce_arr, key_arr) catch {
                return error.AuthenticationFailed;
            };
        },
        .aes_256_gcm => {
            const gcm = std.crypto.aead.aes_gcm.Aes256Gcm;
            var tag_arr: [16]u8 = undefined;
            @memcpy(&tag_arr, tag[0..16]);
            var key_arr: [32]u8 = undefined;
            @memcpy(&key_arr, key[0..32]);
            var nonce_arr: [12]u8 = undefined;
            @memcpy(&nonce_arr, iv[0..12]);

            gcm.decrypt(plaintext, ciphertext, tag_arr, aad, nonce_arr, key_arr) catch {
                return error.AuthenticationFailed;
            };
        },
    }

    // In local deterministic mode, verify SIV
    if (options.verify_context) |ctx| {
        var expected_salt: [SALT_SIZE]u8 = undefined;
        const expected_iv = try allocator.alloc(u8, iv_len);
        defer allocator.free(expected_iv);

        try computeSivParams(allocator, password, plaintext, ctx, enc_options, &expected_salt, expected_iv);

        // Technically, in global deterministic mode, salt wouldn't match the computed one,
        // but IV should. Since we don't know the exact mode at decrypt without an explicit flag,
        // we at least check the IV.
        if (!std.mem.eql(u8, expected_iv, iv)) {
            return error.IntegrityCheckFailed;
        }
    }

    return plaintext;
}

// -----------------------------------------------------------------------------
// Unit Tests
// -----------------------------------------------------------------------------

const testing = std.testing;

// -----------------------------------------------------------------------------
// Test: SIV Parameters Generation
// -----------------------------------------------------------------------------

test "SIV: Deterministic output for same inputs (Local Mode)" {
    const password = "my_secret_password";
    const data = "hello world";
    const context = "path/to/file.txt";

    const options = EncryptOptions{
        .cipher = .aes_256_gcm,
        .kdf = .pbkdf2,
        .hash = .sha256,
        .iterations = 10, // low iterations for tests
        .siv_mode = .{ .local_deterministic = .{ .context = context } },
    };

    var salt1: [SALT_SIZE]u8 = undefined;
    var iv1: [12]u8 = undefined;
    try computeSivParams(testing.allocator, password, data, context, options, &salt1, &iv1);

    var salt2: [SALT_SIZE]u8 = undefined;
    var iv2: [12]u8 = undefined;
    try computeSivParams(testing.allocator, password, data, context, options, &salt2, &iv2);

    try testing.expectEqualSlices(u8, &salt1, &salt2);
    try testing.expectEqualSlices(u8, &iv1, &iv2);
}

test "SIV: Different output for different data (Local Mode)" {
    const password = "my_secret_password";
    const data1 = "hello world";
    const data2 = "hello world!"; // Modified
    const context = "path/to/file.txt";

    const options = EncryptOptions{
        .cipher = .aes_256_gcm,
        .kdf = .pbkdf2,
        .hash = .sha256,
        .iterations = 10,
        .siv_mode = .{ .local_deterministic = .{ .context = context } },
    };

    var salt1: [SALT_SIZE]u8 = undefined;
    var iv1: [12]u8 = undefined;
    try computeSivParams(testing.allocator, password, data1, context, options, &salt1, &iv1);

    var salt2: [SALT_SIZE]u8 = undefined;
    var iv2: [12]u8 = undefined;
    try computeSivParams(testing.allocator, password, data2, context, options, &salt2, &iv2);

    // Both salt and IV should change in local deterministic mode when data changes
    try testing.expect(!std.mem.eql(u8, &salt1, &salt2));
    try testing.expect(!std.mem.eql(u8, &iv1, &iv2));
}

test "SIV: Hash algorithm affects output" {
    const password = "my_secret_password";
    const data = "hello world";
    const context = "path/to/file.txt";

    var salt1: [SALT_SIZE]u8 = undefined;
    var iv1: [12]u8 = undefined;
    try computeSivParams(testing.allocator, password, data, context, .{
        .cipher = .aes_256_gcm,
        .kdf = .pbkdf2,
        .hash = .sha256,
        .iterations = 10,
        .siv_mode = .{ .local_deterministic = .{ .context = context } },
    }, &salt1, &iv1);

    var salt2: [SALT_SIZE]u8 = undefined;
    var iv2: [12]u8 = undefined;
    try computeSivParams(testing.allocator, password, data, context, .{
        .cipher = .aes_256_gcm,
        .kdf = .pbkdf2,
        .hash = .sha512, // Different hash
        .iterations = 10,
        .siv_mode = .{ .local_deterministic = .{ .context = context } },
    }, &salt2, &iv2);

    try testing.expect(!std.mem.eql(u8, &salt1, &salt2));
    try testing.expect(!std.mem.eql(u8, &iv1, &iv2));
}

// -----------------------------------------------------------------------------
// Test: Key Derivation
// -----------------------------------------------------------------------------

test "KDF: PBKDF2 generates expected length" {
    const password = "my_secret_password";
    const salt: [8]u8 = .{ 1, 2, 3, 4, 5, 6, 7, 8 };

    const options = EncryptOptions{
        .cipher = .aes_256_gcm,
        .kdf = .pbkdf2,
        .hash = .sha256,
        .iterations = 100,
        .siv_mode = .random,
    };

    const key = try deriveKey(testing.allocator, password, &salt, 32, options);
    defer testing.allocator.free(key);

    try testing.expectEqual(@as(usize, 32), key.len);
}

test "KDF: Argon2id generates expected length" {
    const password = "my_secret_password";
    const salt: [8]u8 = .{ 1, 2, 3, 4, 5, 6, 7, 8 };

    const options = EncryptOptions{
        .cipher = .chacha20_poly1305,
        .kdf = .argon2id,
        .hash = .sha256,
        .iterations = 1, // fast for testing
        .siv_mode = .random,
    };

    const key = try deriveKey(testing.allocator, password, &salt, 32, options);
    defer testing.allocator.free(key);

    try testing.expectEqual(@as(usize, 32), key.len);
}

// -----------------------------------------------------------------------------
// Test: Encryption & Decryption Roundtrips
// -----------------------------------------------------------------------------

test "Roundtrip: AES-256-GCM / PBKDF2 / SHA-256 (Local Mode)" {
    const password = "strong_password";
    const plaintext = "This is a secret message that needs to be encrypted.";
    const context = "docs/secret.txt";

    const enc_options = EncryptOptions{
        .cipher = .aes_256_gcm,
        .kdf = .pbkdf2,
        .hash = .sha256,
        .iterations = 100,
        .siv_mode = .{ .local_deterministic = .{ .context = context } },
    };

    const ciphertext_b64 = try encrypt(testing.allocator, plaintext, password, enc_options);
    defer testing.allocator.free(ciphertext_b64);

    const dec_options = DecryptOptions{
        .cipher = .aes_256_gcm,
        .kdf = .pbkdf2,
        .hash = .sha256,
        .iterations = 100,
        .verify_context = context,
    };

    const decrypted = try decrypt(testing.allocator, ciphertext_b64, password, dec_options);
    defer testing.allocator.free(decrypted);

    try testing.expectEqualStrings(plaintext, decrypted);
}

test "Roundtrip: ChaCha20-Poly1305 / Argon2id / BLAKE2b (Global Mode)" {
    const password = "strong_password";
    const plaintext = "Another secret message with different algorithms.";
    const context = "docs/other.txt";
    const global_salt: [8]u8 = .{ 8, 7, 6, 5, 4, 3, 2, 1 };

    const enc_options = EncryptOptions{
        .cipher = .chacha20_poly1305,
        .kdf = .argon2id,
        .hash = .blake2b,
        .iterations = 1,
        .siv_mode = .{ .global_deterministic = .{ .salt = global_salt, .context = context } },
    };

    const ciphertext_b64 = try encrypt(testing.allocator, plaintext, password, enc_options);
    defer testing.allocator.free(ciphertext_b64);

    const dec_options = DecryptOptions{
        .cipher = .chacha20_poly1305,
        .kdf = .argon2id,
        .hash = .blake2b,
        .iterations = 1,
        .verify_context = context,
    };

    const decrypted = try decrypt(testing.allocator, ciphertext_b64, password, dec_options);
    defer testing.allocator.free(decrypted);

    try testing.expectEqualStrings(plaintext, decrypted);
}

test "Roundtrip: Empty plaintext" {
    const password = "password";
    const plaintext = "";
    const context = "empty.txt";

    const enc_options = EncryptOptions{
        .cipher = .aes_256_gcm,
        .kdf = .pbkdf2,
        .hash = .sha256,
        .iterations = 10,
        .siv_mode = .{ .local_deterministic = .{ .context = context } },
    };

    const ciphertext_b64 = try encrypt(testing.allocator, plaintext, password, enc_options);
    defer testing.allocator.free(ciphertext_b64);

    const dec_options = DecryptOptions{
        .cipher = .aes_256_gcm,
        .kdf = .pbkdf2,
        .hash = .sha256,
        .iterations = 10,
        .verify_context = context,
    };

    const decrypted = try decrypt(testing.allocator, ciphertext_b64, password, dec_options);
    defer testing.allocator.free(decrypted);

    try testing.expectEqualStrings(plaintext, decrypted);
}

// -----------------------------------------------------------------------------
// Test: Edge Cases and Errors
// -----------------------------------------------------------------------------

test "Error: Wrong password" {
    const password = "correct_password";
    const wrong_password = "wrong_password";
    const plaintext = "Secret data";
    const context = "secret.txt";

    const enc_options = EncryptOptions{
        .cipher = .aes_256_gcm,
        .kdf = .pbkdf2,
        .hash = .sha256,
        .iterations = 10,
        .siv_mode = .{ .local_deterministic = .{ .context = context } },
    };

    const ciphertext_b64 = try encrypt(testing.allocator, plaintext, password, enc_options);
    defer testing.allocator.free(ciphertext_b64);

    const dec_options = DecryptOptions{
        .cipher = .aes_256_gcm,
        .kdf = .pbkdf2,
        .hash = .sha256,
        .iterations = 10,
        .verify_context = context,
    };

    // Should fail with AuthenticationFailed because AEAD MAC check fails
    try testing.expectError(error.AuthenticationFailed, decrypt(testing.allocator, ciphertext_b64, wrong_password, dec_options));
}

test "Error: Wrong context (SIV validation failure)" {
    const password = "password";
    const plaintext = "Secret data";
    const correct_context = "file1.txt";
    const wrong_context = "file2.txt";

    const enc_options = EncryptOptions{
        .cipher = .aes_256_gcm,
        .kdf = .pbkdf2,
        .hash = .sha256,
        .iterations = 10,
        .siv_mode = .{ .local_deterministic = .{ .context = correct_context } },
    };

    const ciphertext_b64 = try encrypt(testing.allocator, plaintext, password, enc_options);
    defer testing.allocator.free(ciphertext_b64);

    const dec_options = DecryptOptions{
        .cipher = .aes_256_gcm,
        .kdf = .pbkdf2,
        .hash = .sha256,
        .iterations = 10,
        .verify_context = wrong_context, // Pass the wrong context for validation
    };

    // If context is wrong, AAD is wrong, so AuthenticationFailed should trigger first during AEAD decrypt
    try testing.expectError(error.AuthenticationFailed, decrypt(testing.allocator, ciphertext_b64, password, dec_options));
}

test "Error: Tampered ciphertext (AEAD MAC failure)" {
    const password = "password";
    const plaintext = "Important data";
    const context = "file.txt";

    const enc_options = EncryptOptions{
        .cipher = .chacha20_poly1305,
        .kdf = .pbkdf2,
        .hash = .sha256,
        .iterations = 10,
        .siv_mode = .{ .local_deterministic = .{ .context = context } },
    };

    const ciphertext_b64 = try encrypt(testing.allocator, plaintext, password, enc_options);
    defer testing.allocator.free(ciphertext_b64);

    // Decode, tamper, and re-encode
    var packet = try testing.allocator.alloc(u8, try std.base64.standard.Decoder.calcSizeForSlice(ciphertext_b64));
    defer testing.allocator.free(packet);
    try std.base64.standard.Decoder.decode(packet, ciphertext_b64);

    // Tamper with the last byte (MAC tag or ciphertext)
    packet[packet.len - 1] ^= 0x01;

    const tampered_b64 = try testing.allocator.alloc(u8, std.base64.standard.Encoder.calcSize(packet.len));
    defer testing.allocator.free(tampered_b64);
    _ = std.base64.standard.Encoder.encode(tampered_b64, packet);

    const dec_options = DecryptOptions{
        .cipher = .chacha20_poly1305,
        .kdf = .pbkdf2,
        .hash = .sha256,
        .iterations = 10,
        .verify_context = context,
    };

    try testing.expectError(error.AuthenticationFailed, decrypt(testing.allocator, tampered_b64, password, dec_options));
}

test "Error: Invalid Headers" {
    const password = "password";
    const dec_options = DecryptOptions{
        .cipher = .aes_256_gcm,
        .kdf = .pbkdf2,
        .hash = .sha256,
        .iterations = 10,
    };

    const raw_bad_header1 = "WrongSalt__12345678IVed__123456789012ciphertext_here";
    var b64_bad_header1: [100]u8 = undefined;
    const b64_bad_header1_len = std.base64.standard.Encoder.encode(&b64_bad_header1, raw_bad_header1).len;

    try testing.expectError(error.MissingSaltHeader, decrypt(testing.allocator, b64_bad_header1[0..b64_bad_header1_len], password, dec_options));

    // Valid Salted__ but missing IVed__
    // Let's craft one manually: "Salted__12345678BadIVHeader..."
    var bad_packet2: [64]u8 = undefined;
    @memcpy(bad_packet2[0..8], "Salted__");
    @memcpy(bad_packet2[8..16], "12345678"); // fake salt
    @memcpy(bad_packet2[16..24], "WrongIV_"); // bad IV header

    var b64_bad_packet2: [200]u8 = undefined;
    const enc_len = std.base64.standard.Encoder.encode(&b64_bad_packet2, bad_packet2[0..32]).len;

    try testing.expectError(error.MissingIVHeader, decrypt(testing.allocator, b64_bad_packet2[0..enc_len], password, dec_options));
}
