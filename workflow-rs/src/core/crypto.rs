//! Authenticated encryption for transparent Git file encryption.
//!
//! Maintains **byte-for-byte packet compatibility** with both the Python
//! `transcrypt` implementation and the Zig `wf crypt` rewrite, so existing
//! encrypted repositories can be decrypted without re-encryption.
//!
//! # Packet format
//!
//! All encrypted data is base64-encoded.  The raw binary packet is:
//!
//! ```text
//! "Salted__" (8 B) | salt (8 B) | "IVed__" (6 B) | iv (12 B) | ciphertext | tag (16 B)
//! ```
//!
//! # Supported algorithms
//!
//! | Cipher | KDF | Hash |
//! |---|---|---|
//! | AES-256-GCM | PBKDF2 (default: 99 989 rounds) | SHA-256 / 384 / 512 |
//! | ChaCha20-Poly1305 | Argon2id (default: 4 iterations) | SHA3-256 / 384 / 512 |
//! | | | BLAKE2b / BLAKE2s |
//!
//! # SIV (Synthetic IV) deterministic mode
//!
//! Encrypting the same plaintext twice always produces the same ciphertext in
//! *local-deterministic* mode, which is essential for `git clean`/`smudge`
//! filters that must be idempotent.  Salt and IV are derived via a
//! S2V-like construction:
//!
//! ```text
//! algo_params = "{digest}:{cipher}:{iterations}:{kdf}"
//! hash_input  = 4BE(len(algo_params)) || algo_params || \x00
//!             | 4BE(len(password))    || password    || \x00
//!             | 4BE(len(context))     || context     || \x00
//!             | plaintext
//! digest      = Hash(hash_input)
//! iv          = digest[0 .. iv_len]
//! salt        = digest[iv_len .. iv_len + 8]
//! ```
//!
//! The `context` is typically the file path, preventing an attacker from
//! swapping two files with different paths but identical content.

use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use thiserror::Error;

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Magic prefix written before the salt in every packet.
pub const SALT_HEADER: &[u8] = b"Salted__";
/// Magic prefix written before the IV in every packet.
pub const IV_HEADER: &[u8] = b"IVed__";
/// Number of salt bytes embedded in each packet.
pub const SALT_SIZE: usize = 8;
/// Number of AEAD authentication-tag bytes appended to the ciphertext.
pub const TAG_SIZE: usize = 16;
/// IV/nonce length for both AES-256-GCM and ChaCha20-Poly1305.
pub const NONCE_SIZE: usize = 12;

// ---------------------------------------------------------------------------
// Algorithm enumerations
// ---------------------------------------------------------------------------

/// Supported AEAD cipher algorithms.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum CipherAlgorithm {
    /// AES-256-GCM (default).
    #[default]
    Aes256Gcm,
    /// ChaCha20-Poly1305.
    ChaCha20Poly1305,
}

impl CipherAlgorithm {
    /// Key length in bytes (both ciphers use 256-bit keys).
    pub const fn key_size(self) -> usize {
        32
    }

    /// The canonical lowercase name used in packet metadata and SIV strings.
    pub const fn as_siv_str(self) -> &'static str {
        match self {
            Self::Aes256Gcm => "aes-256-gcm",
            Self::ChaCha20Poly1305 => "chacha20-poly1305",
        }
    }
}

impl std::str::FromStr for CipherAlgorithm {
    type Err = CryptoError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.to_lowercase().replace('_', "-").as_str() {
            "aes-256-gcm" => Ok(Self::Aes256Gcm),
            "chacha20-poly1305" | "chacha20-poly1305-openssl" => Ok(Self::ChaCha20Poly1305),
            other => Err(CryptoError::UnsupportedCipher(other.to_owned())),
        }
    }
}

/// Supported key derivation functions.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum KdfAlgorithm {
    /// PBKDF2 (default; 99 989 rounds for backward compatibility).
    #[default]
    Pbkdf2,
    /// Argon2id (memory-hard; default 4 time-cost iterations, 128 MiB).
    Argon2id,
}

impl KdfAlgorithm {
    /// Returns the default iteration / time-cost count for this KDF.
    ///
    /// `PBKDF2` default (99 989) matches the legacy Python transcrypt value
    /// ensuring existing encrypted repositories can be decrypted without any
    /// explicit configuration.
    pub const fn default_iterations(self) -> u32 {
        match self {
            Self::Pbkdf2 => 99_989,
            Self::Argon2id => 4,
        }
    }

    /// Canonical name used in SIV strings.
    pub const fn as_siv_str(self) -> &'static str {
        match self {
            Self::Pbkdf2 => "pbkdf2",
            Self::Argon2id => "argon2id",
        }
    }
}

impl std::str::FromStr for KdfAlgorithm {
    type Err = CryptoError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.to_lowercase().as_str() {
            "pbkdf2" => Ok(Self::Pbkdf2),
            "argon2id" => Ok(Self::Argon2id),
            other => Err(CryptoError::UnsupportedKdf(other.to_owned())),
        }
    }
}

/// Supported cryptographic hash algorithms.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum HashAlgorithm {
    /// SHA-256 (default).
    #[default]
    Sha256,
    Sha384,
    Sha512,
    Sha3_256,
    Sha3_384,
    Sha3_512,
    /// BLAKE2b (512-bit output).
    Blake2b,
    /// BLAKE2s (256-bit output).
    Blake2s,
}

impl HashAlgorithm {
    /// Canonical name used in SIV strings — must match the Python/Zig format
    /// exactly for cross-implementation compatibility.
    pub const fn as_siv_str(self) -> &'static str {
        match self {
            Self::Sha256 => "sha256",
            Self::Sha384 => "sha384",
            Self::Sha512 => "sha512",
            Self::Sha3_256 => "sha3256",
            Self::Sha3_384 => "sha3384",
            Self::Sha3_512 => "sha3512",
            Self::Blake2b => "blake2b",
            Self::Blake2s => "blake2s",
        }
    }
}

impl std::str::FromStr for HashAlgorithm {
    type Err = CryptoError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let normalised = s.to_lowercase().replace(['-', '_'], "");
        match normalised.as_str() {
            "sha256" => Ok(Self::Sha256),
            "sha384" => Ok(Self::Sha384),
            "sha512" => Ok(Self::Sha512),
            "sha3256" | "sha3-256" => Ok(Self::Sha3_256),
            "sha3384" | "sha3-384" => Ok(Self::Sha3_384),
            "sha3512" | "sha3-512" => Ok(Self::Sha3_512),
            "blake2b" | "blake2b512" => Ok(Self::Blake2b),
            "blake2s" | "blake2s256" => Ok(Self::Blake2s),
            other => Err(CryptoError::UnsupportedHash(other.to_owned())),
        }
    }
}

// ---------------------------------------------------------------------------
// SIV mode
// ---------------------------------------------------------------------------

/// Controls how the salt and IV/nonce are produced for each encryption.
#[derive(Debug, Clone)]
pub enum SivMode {
    /// **Local-deterministic** (the standard transcrypt mode).
    ///
    /// Both salt *and* IV are derived from the content via the SIV
    /// construction.  `context` is typically the file path; it is also used
    /// as the AEAD additional-data (AAD) binding the ciphertext to a specific
    /// file, preventing silent file-swap attacks.
    LocalDeterministic { context: String },
    /// **Global-deterministic** (wf extension, not in Python transcrypt).
    ///
    /// The salt is fixed externally (e.g. derived once per repository from a
    /// global secret).  Only the IV is derived from the plaintext hash.  This
    /// trades per-file salt uniqueness for the ability to detect duplicate
    /// files even before decryption.
    GlobalDeterministic {
        /// Pre-computed 8-byte salt, shared across all files in a context.
        salt: [u8; SALT_SIZE],
        context: String,
    },
    /// **Random** — CSPRNG-generated salt and IV.
    ///
    /// Maximum security; output is non-deterministic.  Cannot be used for
    /// `git clean`/`smudge` filters because re-encrypting an unchanged file
    /// would produce a different ciphertext and trigger spurious diff noise.
    Random,
}

// ---------------------------------------------------------------------------
// Options structs
// ---------------------------------------------------------------------------

/// Configuration for an encryption operation.
#[derive(Debug, Clone)]
pub struct EncryptOptions {
    pub cipher: CipherAlgorithm,
    pub kdf: KdfAlgorithm,
    pub hash: HashAlgorithm,
    /// Explicit iteration count; falls back to [`KdfAlgorithm::default_iterations`].
    pub iterations: Option<u32>,
    pub siv_mode: SivMode,
}

impl Default for EncryptOptions {
    fn default() -> Self {
        Self {
            cipher: CipherAlgorithm::Aes256Gcm,
            kdf: KdfAlgorithm::Pbkdf2,
            hash: HashAlgorithm::Sha256,
            iterations: None,
            siv_mode: SivMode::Random,
        }
    }
}

/// Configuration for a decryption operation.
#[derive(Debug, Clone, Default)]
pub struct DecryptOptions {
    pub cipher: CipherAlgorithm,
    pub kdf: KdfAlgorithm,
    pub hash: HashAlgorithm,
    /// Must match the value used during encryption.
    pub iterations: Option<u32>,
    /// When `Some`, the context bytes are used as AEAD AAD and the SIV
    /// integrity check is performed after decryption.
    pub verify_context: Option<String>,
}

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

/// Errors that can arise during encryption or decryption.
#[derive(Debug, Error)]
pub enum CryptoError {
    #[error("unsupported cipher: {0}")]
    UnsupportedCipher(String),
    #[error("unsupported KDF: {0}")]
    UnsupportedKdf(String),
    #[error("unsupported hash algorithm: {0}")]
    UnsupportedHash(String),
    #[error("AEAD encryption failed")]
    EncryptionFailed,
    #[error("AEAD authentication failed — wrong password or tampered data")]
    AuthenticationFailed,
    #[error("SIV integrity check failed — content may have been tampered with")]
    IntegrityCheckFailed,
    #[error("missing 'Salted__' header in encrypted packet")]
    MissingSaltHeader,
    #[error("missing 'IVed__' header in encrypted packet")]
    MissingIVHeader,
    #[error("encrypted packet is too short")]
    DataTooShort,
    #[error("hash digest ({digest_len} B) too short to produce SIV material ({needed} B)")]
    DigestTooShort { digest_len: usize, needed: usize },
    #[error("invalid base64: {0}")]
    Base64(#[from] base64::DecodeError),
    #[error("KDF error: {0}")]
    Kdf(String),
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Encrypts `plaintext` with `password` using the given options.
///
/// Returns the complete packet as a base64 string ready to be written to a
/// file or piped through a Git filter.
pub fn encrypt(
    plaintext: &[u8],
    password: &str,
    options: &EncryptOptions,
) -> Result<String, CryptoError> {
    let iters = options
        .iterations
        .unwrap_or_else(|| options.kdf.default_iterations());

    let mut salt = [0u8; SALT_SIZE];
    let mut iv = [0u8; NONCE_SIZE];
    let aad: &[u8];

    // 1. Determine salt, IV, and AAD
    match &options.siv_mode {
        SivMode::LocalDeterministic { context } => {
            compute_siv_params(
                password.as_bytes(),
                plaintext,
                context.as_bytes(),
                options.hash,
                options.cipher,
                iters,
                options.kdf,
                &mut salt,
                &mut iv,
            )?;
            aad = context.as_bytes();
        }
        SivMode::GlobalDeterministic { salt: gs, context } => {
            salt = *gs;
            // IV is still derived from the plaintext; we discard the computed salt.
            let mut tmp_salt = [0u8; SALT_SIZE];
            compute_siv_params(
                password.as_bytes(),
                plaintext,
                context.as_bytes(),
                options.hash,
                options.cipher,
                iters,
                options.kdf,
                &mut tmp_salt,
                &mut iv,
            )?;
            aad = context.as_bytes();
        }
        SivMode::Random => {
            use rand::RngCore;
            rand::thread_rng().fill_bytes(&mut salt);
            rand::thread_rng().fill_bytes(&mut iv);
            aad = b"";
        }
    }

    // 2. Derive key
    let key = derive_key(password.as_bytes(), &salt, options.hash, options.kdf, iters)?;

    // 3. AEAD encrypt — returns ciphertext || tag
    let ciphertext_with_tag = aead_encrypt(options.cipher, &key, &iv, plaintext, aad)?;

    // 4. Assemble packet
    let mut packet = Vec::with_capacity(
        SALT_HEADER.len() + SALT_SIZE + IV_HEADER.len() + NONCE_SIZE + ciphertext_with_tag.len(),
    );
    packet.extend_from_slice(SALT_HEADER);
    packet.extend_from_slice(&salt);
    packet.extend_from_slice(IV_HEADER);
    packet.extend_from_slice(&iv);
    packet.extend_from_slice(&ciphertext_with_tag);

    // 5. Base64-encode
    Ok(BASE64.encode(&packet))
}

/// Decrypts a base64-encoded packet produced by [`encrypt`].
///
/// Returns the raw plaintext bytes.
pub fn decrypt(
    ciphertext_b64: &str,
    password: &str,
    options: &DecryptOptions,
) -> Result<Vec<u8>, CryptoError> {
    // 1. Base64-decode
    let packet = BASE64.decode(ciphertext_b64.trim())?;

    let mut offset = 0;

    // 2. Parse salt
    if !packet
        .get(offset..)
        .map_or(false, |s| s.starts_with(SALT_HEADER))
    {
        return Err(CryptoError::MissingSaltHeader);
    }
    offset += SALT_HEADER.len();

    if packet.len() < offset + SALT_SIZE {
        return Err(CryptoError::DataTooShort);
    }
    let salt: [u8; SALT_SIZE] = packet[offset..offset + SALT_SIZE].try_into().unwrap();
    offset += SALT_SIZE;

    // 3. Parse IV
    if !packet
        .get(offset..)
        .map_or(false, |s| s.starts_with(IV_HEADER))
    {
        return Err(CryptoError::MissingIVHeader);
    }
    offset += IV_HEADER.len();

    if packet.len() < offset + NONCE_SIZE {
        return Err(CryptoError::DataTooShort);
    }
    let iv: [u8; NONCE_SIZE] = packet[offset..offset + NONCE_SIZE].try_into().unwrap();
    offset += NONCE_SIZE;

    // 4. Remaining bytes = ciphertext || tag
    let ciphertext_with_tag = &packet[offset..];
    if ciphertext_with_tag.len() < TAG_SIZE {
        return Err(CryptoError::DataTooShort);
    }

    // 5. Derive key
    let iters = options
        .iterations
        .unwrap_or_else(|| options.kdf.default_iterations());
    let key = derive_key(password.as_bytes(), &salt, options.hash, options.kdf, iters)?;

    // 6. AEAD decrypt
    let aad: &[u8] = options
        .verify_context
        .as_deref()
        .map(str::as_bytes)
        .unwrap_or(b"");
    let plaintext = aead_decrypt(options.cipher, &key, &iv, ciphertext_with_tag, aad)?;

    // 7. Optional SIV integrity check
    //
    // After a successful AEAD decryption we additionally verify that the IV
    // embedded in the packet matches what we would deterministically derive
    // from the decrypted content.  This catches cases where the ciphertext was
    // valid AEAD but the IV has been substituted (possible in theory if the
    // key is compromised).  Only the IV is checked (not the salt) so that the
    // check works for both local- and global-deterministic modes.
    if let Some(context) = &options.verify_context {
        let enc_opts = EncryptOptions {
            cipher: options.cipher,
            kdf: options.kdf,
            hash: options.hash,
            iterations: options.iterations,
            siv_mode: SivMode::LocalDeterministic {
                context: context.clone(),
            },
        };
        let mut expected_salt = [0u8; SALT_SIZE];
        let mut expected_iv = [0u8; NONCE_SIZE];
        compute_siv_params(
            password.as_bytes(),
            &plaintext,
            context.as_bytes(),
            enc_opts.hash,
            enc_opts.cipher,
            iters,
            enc_opts.kdf,
            &mut expected_salt,
            &mut expected_iv,
        )?;
        if iv != expected_iv {
            return Err(CryptoError::IntegrityCheckFailed);
        }
    }

    Ok(plaintext)
}

// ---------------------------------------------------------------------------
// SIV parameter computation
// ---------------------------------------------------------------------------

/// Computes deterministic salt and IV from the content and algorithm parameters.
///
/// The construction is identical across Python, Zig, and this Rust
/// implementation, ensuring cross-language compatibility.
pub fn compute_siv_params(
    password: &[u8],
    data: &[u8],
    context: &[u8],
    hash: HashAlgorithm,
    cipher: CipherAlgorithm,
    iterations: u32,
    kdf: KdfAlgorithm,
    out_salt: &mut [u8; SALT_SIZE],
    out_iv: &mut [u8; NONCE_SIZE],
) -> Result<(), CryptoError> {
    let algo_params = format!(
        "{}:{}:{}:{}",
        hash.as_siv_str(),
        cipher.as_siv_str(),
        iterations,
        kdf.as_siv_str(),
    );

    // Helper that feeds all fields into a `digest::Update` implementor. The
    // `digest` traits are used via `sha2`'s re-export so we don't need a direct
    // dependency on the `digest` crate (all RustCrypto hashes share it).
    fn feed<H: sha2::digest::Update>(h: &mut H, algo: &[u8], pwd: &[u8], ctx: &[u8], data: &[u8]) {
        let sep = b"\x00";
        h.update(&(algo.len() as u32).to_be_bytes());
        h.update(algo);
        h.update(sep);
        h.update(&(pwd.len() as u32).to_be_bytes());
        h.update(pwd);
        h.update(sep);
        h.update(&(ctx.len() as u32).to_be_bytes());
        h.update(ctx);
        h.update(sep);
        h.update(data);
    }

    // Helper that extracts iv || salt from the leading bytes of a digest.
    fn extract(
        digest_bytes: &[u8],
        out_iv: &mut [u8; NONCE_SIZE],
        out_salt: &mut [u8; SALT_SIZE],
    ) -> Result<(), CryptoError> {
        let needed = NONCE_SIZE + SALT_SIZE;
        if digest_bytes.len() < needed {
            return Err(CryptoError::DigestTooShort {
                digest_len: digest_bytes.len(),
                needed,
            });
        }
        out_iv.copy_from_slice(&digest_bytes[..NONCE_SIZE]);
        out_salt.copy_from_slice(&digest_bytes[NONCE_SIZE..NONCE_SIZE + SALT_SIZE]);
        Ok(())
    }

    let ap = algo_params.as_bytes();

    match hash {
        HashAlgorithm::Sha256 => {
            use sha2::{Digest, Sha256};
            let mut h = Sha256::new();
            feed(&mut h, ap, password, context, data);
            extract(h.finalize().as_slice(), out_iv, out_salt)
        }
        HashAlgorithm::Sha384 => {
            use sha2::{Digest, Sha384};
            let mut h = Sha384::new();
            feed(&mut h, ap, password, context, data);
            extract(h.finalize().as_slice(), out_iv, out_salt)
        }
        HashAlgorithm::Sha512 => {
            use sha2::{Digest, Sha512};
            let mut h = Sha512::new();
            feed(&mut h, ap, password, context, data);
            extract(h.finalize().as_slice(), out_iv, out_salt)
        }
        HashAlgorithm::Sha3_256 => {
            use sha3::{Digest, Sha3_256};
            let mut h = Sha3_256::new();
            feed(&mut h, ap, password, context, data);
            extract(h.finalize().as_slice(), out_iv, out_salt)
        }
        HashAlgorithm::Sha3_384 => {
            use sha3::{Digest, Sha3_384};
            let mut h = Sha3_384::new();
            feed(&mut h, ap, password, context, data);
            extract(h.finalize().as_slice(), out_iv, out_salt)
        }
        HashAlgorithm::Sha3_512 => {
            use sha3::{Digest, Sha3_512};
            let mut h = Sha3_512::new();
            feed(&mut h, ap, password, context, data);
            extract(h.finalize().as_slice(), out_iv, out_salt)
        }
        HashAlgorithm::Blake2b => {
            use blake2::{Blake2b512, Digest};
            let mut h = Blake2b512::new();
            feed(&mut h, ap, password, context, data);
            extract(h.finalize().as_slice(), out_iv, out_salt)
        }
        HashAlgorithm::Blake2s => {
            use blake2::{Blake2s256, Digest};
            let mut h = Blake2s256::new();
            feed(&mut h, ap, password, context, data);
            extract(h.finalize().as_slice(), out_iv, out_salt)
        }
    }
}

// ---------------------------------------------------------------------------
// Key derivation
// ---------------------------------------------------------------------------

fn derive_key(
    password: &[u8],
    salt: &[u8; SALT_SIZE],
    hash: HashAlgorithm,
    kdf: KdfAlgorithm,
    iters: u32,
) -> Result<Vec<u8>, CryptoError> {
    let key_len = CipherAlgorithm::Aes256Gcm.key_size(); // Both ciphers use 32 B
    let mut key = vec![0u8; key_len];

    match kdf {
        KdfAlgorithm::Pbkdf2 => {
            // In pbkdf2 0.12, pbkdf2_hmac takes the *hash* type as the type
            // parameter (D: Digest + BlockSizeUser + Clone) and wraps it in
            // HMAC internally.  Do NOT pass Hmac<H> — pass H directly.
            use pbkdf2::pbkdf2_hmac;

            match hash {
                HashAlgorithm::Sha256 => {
                    pbkdf2_hmac::<sha2::Sha256>(password, salt, iters, &mut key);
                }
                HashAlgorithm::Sha384 => {
                    pbkdf2_hmac::<sha2::Sha384>(password, salt, iters, &mut key);
                }
                HashAlgorithm::Sha512 => {
                    pbkdf2_hmac::<sha2::Sha512>(password, salt, iters, &mut key);
                }
                HashAlgorithm::Sha3_256 => {
                    pbkdf2_hmac::<sha3::Sha3_256>(password, salt, iters, &mut key);
                }
                HashAlgorithm::Sha3_384 => {
                    pbkdf2_hmac::<sha3::Sha3_384>(password, salt, iters, &mut key);
                }
                HashAlgorithm::Sha3_512 => {
                    pbkdf2_hmac::<sha3::Sha3_512>(password, salt, iters, &mut key);
                }
                HashAlgorithm::Blake2b | HashAlgorithm::Blake2s => {
                    // BLAKE2 uses a "Lazy" BufferKind internally, which is
                    // incompatible with pbkdf2_hmac's Eager requirement.
                    // BLAKE2 is supported for SIV hashing; use SHA-2 or
                    // SHA-3 when a PBKDF2 + BLAKE2 combination is needed.
                    return Err(CryptoError::UnsupportedHash(
                        "BLAKE2 cannot be used as the PBKDF2 PRF; \
                         choose sha256/sha512/sha3-256 or switch to argon2id"
                            .to_owned(),
                    ));
                }
            }
        }

        KdfAlgorithm::Argon2id => {
            use argon2::{Algorithm, Argon2, Params, Version};

            let params = Params::new(
                131_072, // m_cost: 128 MiB (matches Python ARGON2_DEFAULT_MEMORY)
                iters,   // t_cost
                2,       // p_cost: 2 lanes (matches Python ARGON2_DEFAULT_LANES)
                Some(key_len),
            )
            .map_err(|e| CryptoError::Kdf(e.to_string()))?;

            Argon2::new(Algorithm::Argon2id, Version::V0x13, params)
                .hash_password_into(password, salt, &mut key)
                .map_err(|e| CryptoError::Kdf(e.to_string()))?;
        }
    }

    Ok(key)
}

// ---------------------------------------------------------------------------
// AEAD helpers
// ---------------------------------------------------------------------------

fn aead_encrypt(
    cipher: CipherAlgorithm,
    key: &[u8],
    iv: &[u8; NONCE_SIZE],
    plaintext: &[u8],
    aad: &[u8],
) -> Result<Vec<u8>, CryptoError> {
    match cipher {
        CipherAlgorithm::Aes256Gcm => {
            use aes_gcm::{
                aead::{Aead, KeyInit, Payload},
                Aes256Gcm, Nonce,
            };
            let cipher =
                Aes256Gcm::new_from_slice(key).map_err(|_| CryptoError::EncryptionFailed)?;
            let nonce = Nonce::from_slice(iv);
            cipher
                .encrypt(
                    nonce,
                    Payload {
                        msg: plaintext,
                        aad,
                    },
                )
                .map_err(|_| CryptoError::EncryptionFailed)
        }
        CipherAlgorithm::ChaCha20Poly1305 => {
            use chacha20poly1305::{
                aead::{Aead, KeyInit, Payload},
                ChaCha20Poly1305, Nonce,
            };
            let cipher =
                ChaCha20Poly1305::new_from_slice(key).map_err(|_| CryptoError::EncryptionFailed)?;
            let nonce = Nonce::from_slice(iv);
            cipher
                .encrypt(
                    nonce,
                    Payload {
                        msg: plaintext,
                        aad,
                    },
                )
                .map_err(|_| CryptoError::EncryptionFailed)
        }
    }
}

fn aead_decrypt(
    cipher: CipherAlgorithm,
    key: &[u8],
    iv: &[u8; NONCE_SIZE],
    ciphertext_with_tag: &[u8],
    aad: &[u8],
) -> Result<Vec<u8>, CryptoError> {
    match cipher {
        CipherAlgorithm::Aes256Gcm => {
            use aes_gcm::{
                aead::{Aead, KeyInit, Payload},
                Aes256Gcm, Nonce,
            };
            let cipher =
                Aes256Gcm::new_from_slice(key).map_err(|_| CryptoError::AuthenticationFailed)?;
            let nonce = Nonce::from_slice(iv);
            cipher
                .decrypt(
                    nonce,
                    Payload {
                        msg: ciphertext_with_tag,
                        aad,
                    },
                )
                .map_err(|_| CryptoError::AuthenticationFailed)
        }
        CipherAlgorithm::ChaCha20Poly1305 => {
            use chacha20poly1305::{
                aead::{Aead, KeyInit, Payload},
                ChaCha20Poly1305, Nonce,
            };
            let cipher = ChaCha20Poly1305::new_from_slice(key)
                .map_err(|_| CryptoError::AuthenticationFailed)?;
            let nonce = Nonce::from_slice(iv);
            cipher
                .decrypt(
                    nonce,
                    Payload {
                        msg: ciphertext_with_tag,
                        aad,
                    },
                )
                .map_err(|_| CryptoError::AuthenticationFailed)
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // -----------------------------------------------------------------------
    // SIV determinism
    // -----------------------------------------------------------------------

    #[test]
    fn siv_is_deterministic_for_same_inputs() {
        let (mut s1, mut iv1) = ([0u8; SALT_SIZE], [0u8; NONCE_SIZE]);
        let (mut s2, mut iv2) = ([0u8; SALT_SIZE], [0u8; NONCE_SIZE]);
        compute_siv_params(
            b"password",
            b"hello world",
            b"path/to/file.txt",
            HashAlgorithm::Sha256,
            CipherAlgorithm::Aes256Gcm,
            100,
            KdfAlgorithm::Pbkdf2,
            &mut s1,
            &mut iv1,
        )
        .unwrap();
        compute_siv_params(
            b"password",
            b"hello world",
            b"path/to/file.txt",
            HashAlgorithm::Sha256,
            CipherAlgorithm::Aes256Gcm,
            100,
            KdfAlgorithm::Pbkdf2,
            &mut s2,
            &mut iv2,
        )
        .unwrap();
        assert_eq!(s1, s2);
        assert_eq!(iv1, iv2);
    }

    #[test]
    fn siv_changes_when_data_changes() {
        let (mut s1, mut iv1) = ([0u8; SALT_SIZE], [0u8; NONCE_SIZE]);
        let (mut s2, mut iv2) = ([0u8; SALT_SIZE], [0u8; NONCE_SIZE]);
        compute_siv_params(
            b"password",
            b"data1",
            b"ctx",
            HashAlgorithm::Sha256,
            CipherAlgorithm::Aes256Gcm,
            100,
            KdfAlgorithm::Pbkdf2,
            &mut s1,
            &mut iv1,
        )
        .unwrap();
        compute_siv_params(
            b"password",
            b"data2",
            b"ctx",
            HashAlgorithm::Sha256,
            CipherAlgorithm::Aes256Gcm,
            100,
            KdfAlgorithm::Pbkdf2,
            &mut s2,
            &mut iv2,
        )
        .unwrap();
        assert_ne!(s1, s2);
        assert_ne!(iv1, iv2);
    }

    // -----------------------------------------------------------------------
    // KDF
    // -----------------------------------------------------------------------

    #[test]
    fn pbkdf2_produces_correct_key_length() {
        let key = derive_key(
            b"pass",
            &[1u8; 8],
            HashAlgorithm::Sha256,
            KdfAlgorithm::Pbkdf2,
            100,
        )
        .unwrap();
        assert_eq!(key.len(), 32);
    }

    #[test]
    fn argon2id_produces_correct_key_length() {
        let key = derive_key(
            b"pass",
            &[1u8; 8],
            HashAlgorithm::Sha256,
            KdfAlgorithm::Argon2id,
            1,
        )
        .unwrap();
        assert_eq!(key.len(), 32);
    }

    // -----------------------------------------------------------------------
    // Encrypt / decrypt roundtrips
    // -----------------------------------------------------------------------

    fn roundtrip(
        plaintext: &[u8],
        cipher: CipherAlgorithm,
        kdf: KdfAlgorithm,
        hash: HashAlgorithm,
        iters: u32,
        context: &str,
    ) {
        let enc_opts = EncryptOptions {
            cipher,
            kdf,
            hash,
            iterations: Some(iters),
            siv_mode: SivMode::LocalDeterministic {
                context: context.to_owned(),
            },
        };
        let ct = encrypt(plaintext, "strong_pass", &enc_opts).unwrap();

        let dec_opts = DecryptOptions {
            cipher,
            kdf,
            hash,
            iterations: Some(iters),
            verify_context: Some(context.to_owned()),
        };
        let pt = decrypt(&ct, "strong_pass", &dec_opts).unwrap();
        assert_eq!(pt, plaintext);
    }

    #[test]
    fn roundtrip_aes256gcm_pbkdf2_sha256() {
        roundtrip(
            b"Secret message",
            CipherAlgorithm::Aes256Gcm,
            KdfAlgorithm::Pbkdf2,
            HashAlgorithm::Sha256,
            100,
            "docs/secret.txt",
        );
    }

    #[test]
    fn roundtrip_chacha20poly1305_argon2id_blake2b() {
        roundtrip(
            b"Another secret",
            CipherAlgorithm::ChaCha20Poly1305,
            KdfAlgorithm::Argon2id,
            HashAlgorithm::Blake2b,
            1,
            "data/file.bin",
        );
    }

    #[test]
    fn roundtrip_empty_plaintext() {
        roundtrip(
            b"",
            CipherAlgorithm::Aes256Gcm,
            KdfAlgorithm::Pbkdf2,
            HashAlgorithm::Sha256,
            10,
            "empty.txt",
        );
    }

    #[test]
    fn random_mode_roundtrip() {
        let enc_opts = EncryptOptions {
            siv_mode: SivMode::Random,
            iterations: Some(10),
            ..Default::default()
        };
        let ct = encrypt(b"hello random", "pass", &enc_opts).unwrap();
        let dec_opts = DecryptOptions {
            iterations: Some(10),
            ..Default::default()
        };
        let pt = decrypt(&ct, "pass", &dec_opts).unwrap();
        assert_eq!(pt, b"hello random");
    }

    #[test]
    fn deterministic_mode_is_idempotent() {
        let opts = EncryptOptions {
            iterations: Some(10),
            siv_mode: SivMode::LocalDeterministic {
                context: "file.txt".to_owned(),
            },
            ..Default::default()
        };
        let ct1 = encrypt(b"same content", "pass", &opts).unwrap();
        let ct2 = encrypt(b"same content", "pass", &opts).unwrap();
        assert_eq!(ct1, ct2);
    }

    // -----------------------------------------------------------------------
    // Error cases
    // -----------------------------------------------------------------------

    #[test]
    fn wrong_password_fails_with_auth_error() {
        let enc_opts = EncryptOptions {
            iterations: Some(10),
            siv_mode: SivMode::LocalDeterministic {
                context: "f.txt".to_owned(),
            },
            ..Default::default()
        };
        let ct = encrypt(b"secret", "correct", &enc_opts).unwrap();
        let dec_opts = DecryptOptions {
            iterations: Some(10),
            verify_context: Some("f.txt".to_owned()),
            ..Default::default()
        };
        let err = decrypt(&ct, "wrong", &dec_opts).unwrap_err();
        assert!(matches!(err, CryptoError::AuthenticationFailed));
    }

    #[test]
    fn wrong_context_fails_authentication() {
        let enc_opts = EncryptOptions {
            iterations: Some(10),
            siv_mode: SivMode::LocalDeterministic {
                context: "file1.txt".to_owned(),
            },
            ..Default::default()
        };
        let ct = encrypt(b"data", "pass", &enc_opts).unwrap();
        let dec_opts = DecryptOptions {
            iterations: Some(10),
            verify_context: Some("file2.txt".to_owned()), // wrong context → wrong AAD
            ..Default::default()
        };
        let err = decrypt(&ct, "pass", &dec_opts).unwrap_err();
        assert!(matches!(err, CryptoError::AuthenticationFailed));
    }

    #[test]
    fn tampered_ciphertext_fails_authentication() {
        let enc_opts = EncryptOptions {
            iterations: Some(10),
            siv_mode: SivMode::LocalDeterministic {
                context: "f.txt".to_owned(),
            },
            ..Default::default()
        };
        let ct_b64 = encrypt(b"important data", "pass", &enc_opts).unwrap();

        // Decode, flip a byte in the ciphertext body, re-encode
        let mut raw = BASE64.decode(&ct_b64).unwrap();
        let last = raw.len() - 1;
        raw[last] ^= 0x01;
        let tampered = BASE64.encode(&raw);

        let dec_opts = DecryptOptions {
            iterations: Some(10),
            verify_context: Some("f.txt".to_owned()),
            ..Default::default()
        };
        let err = decrypt(&tampered, "pass", &dec_opts).unwrap_err();
        assert!(matches!(err, CryptoError::AuthenticationFailed));
    }

    #[test]
    fn missing_salt_header_returns_error() {
        let garbage = BASE64.encode(b"WrongHeader12345678IVed__012345678901ciphertext");
        let dec_opts = DecryptOptions {
            iterations: Some(10),
            ..Default::default()
        };
        let err = decrypt(&garbage, "pass", &dec_opts).unwrap_err();
        assert!(matches!(err, CryptoError::MissingSaltHeader));
    }

    #[test]
    fn missing_iv_header_returns_error() {
        let mut raw = Vec::new();
        raw.extend_from_slice(b"Salted__");
        raw.extend_from_slice(b"12345678"); // salt
        raw.extend_from_slice(b"BadIVHdr"); // wrong IV header
        let encoded = BASE64.encode(&raw);
        let dec_opts = DecryptOptions {
            iterations: Some(10),
            ..Default::default()
        };
        let err = decrypt(&encoded, "pass", &dec_opts).unwrap_err();
        assert!(matches!(err, CryptoError::MissingIVHeader));
    }
}
