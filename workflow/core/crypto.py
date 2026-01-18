import base64
import os
import re
from typing import Optional, Union, Tuple, Type

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
# Try to import AEAD modes directly if available
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
    HAS_AEAD_PRIMITIVES = True
except ImportError:
    HAS_AEAD_PRIMITIVES = False

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.backends import default_backend

# Constants matching OpenSSL / Transcrypt behavior
SALT_HEADER = b"Salted__"
IV_HEADER = b"IVed__"
DEFAULT_ITERATIONS = 99989
DEFAULT_KDF = "pbkdf2"
# Argon2 Constants
ARGON2_DEFAULT_ITERATIONS = 4
ARGON2_DEFAULT_MEMORY = 131072  # 128 MiB
ARGON2_DEFAULT_LANES = 2

# Scrypt Constants
SCRYPT_DEFAULT_LENGTH = 32
SCRYPT_DEFAULT_N = 2**15  # 128 * r * N MiB
SCRYPT_DEFAULT_R = 8
SCRYPT_DEFAULT_P = 2

DEFAULT_DIGEST = "sha256"
DEFAULT_CIPHER = "aes-256-cbc"
SALT_SIZE = 8
BLOCK_SIZE_AES = 16
NONCE_SIZE_GCM = 12
NONCE_SIZE_CHACHA = 12

def get_digest_algorithm(name: str):
    name = name.lower().replace("-", "").replace("_", "")
    if name == "sha256":
        return hashes.SHA256()
    elif name == "sha1":
        return hashes.SHA1()
    elif name == "sha224":
        return hashes.SHA224()
    elif name == "sha384":
        return hashes.SHA384()
    elif name == "sha512":
        return hashes.SHA512()
    elif name == "sha3224":
        return hashes.SHA3_224()
    elif name == "sha3256":
        return hashes.SHA3_256()
    elif name == "sha3384":
        return hashes.SHA3_384()
    elif name == "sha3512":
        return hashes.SHA3_512()
    elif name == "md5":
        return hashes.MD5()
    elif name == "blake2b":
        return hashes.BLAKE2b(64)
    elif name == "blake2s":
        return hashes.BLAKE2s(32)
    else:
        raise ValueError(f"Unsupported digest: {name}")

def _parse_cipher_name(name: str) -> Tuple[str, Union[type,modes.Mode,str], int]:
    """
    Parses a cipher string.
    Returns (AlgorithmName, ModeOrType, KeyLengthInBytes)
    AlgorithmName: 'aes', 'chacha20', 'aria'
    ModeOrType: modes.CBC, 'gcm', 'poly1305', etc
    """
    # Normalize
    name = name.lower()

    # Special Case: ChaCha20-Poly1305
    if name == 'chacha20-poly1305' or name == 'chacha20-poly1305-openssl':
        return 'chacha20', 'poly1305', 32

    parts = name.split('-')

    # Handle GCM which might be aes-256-gcm
    if len(parts) != 3:
         raise ValueError(f"Invalid cipher format: {name}. Expected algo-bits-mode.")

    algo_str, bits_str, mode_str = parts

    # 1. Algorithm
    if algo_str == 'aes':
        algo_name = 'aes'
    elif algo_str == 'sm4':
        algo_name = 'sm4'
    elif algo_str == 'camellia':
        algo_name = 'camellia'
    else:
        # ARIA dropped per user request, and others not supported
        raise ValueError(f"Unsupported algorithm: {algo_str}")

    # 2. Key Length
    try:
        bits = int(bits_str)
    except ValueError:
         raise ValueError(f"Invalid key bits: {bits_str}")

    if algo_name == 'sm4' and bits != 128:
        raise ValueError(f"SM4 only supports 128-bit keys, got {bits}")

    valid_bits = [128, 192, 256]
    if bits not in valid_bits:
        raise ValueError(f"Unsupported key length: {bits}. Must be one of {valid_bits}")
    key_len = bits // 8

    # 3. Mode
    if mode_str == 'gcm':
        mode_val = 'gcm'
    elif mode_str == 'cbc':
        mode_val = modes.CBC
    elif mode_str == 'ctr':
        mode_val = modes.CTR
    elif mode_str == 'cfb':
        mode_val = modes.CFB
    elif mode_str == 'ecb':
        mode_val = modes.ECB
    elif mode_str == 'ofb':
        mode_val = modes.OFB
    else:
        raise ValueError(f"Unsupported mode: {mode_str}")

    return algo_name, mode_val, key_len

def derive_key(
    password: Union[str, bytes],
    salt: bytes,
    iterations: int,
    digest_name: str,
    key_length: int,
    kdf_name: str = DEFAULT_KDF,
    memory_cost: int = ARGON2_DEFAULT_MEMORY,
    lanes: int = ARGON2_DEFAULT_LANES
) -> bytes:
    """
    Derive a key from a password and salt using PBKDF2 or Argon2id.
    """
    if isinstance(password, str):
        password = password.encode('utf-8')

    if kdf_name == "argon2id":
        kdf = Argon2id(
            salt=salt,
            length=key_length,
            iterations=iterations,
            lanes=lanes,
            memory_cost=memory_cost,
        )
        return kdf.derive(password)

    if kdf_name == "scrypt":
        # Scrypt doesn't use 'iterations' in the same way PBKDF2 does.
        # It uses N (cost), r (block size), p (parallelization).
        # We need to map 'iterations' to one of these or just use defaults modified by it?
        # Typically 'iterations' maps to N (CPU/Memory cost) in simple mappings, 
        # but N must be power of 2. 
        # For simplicity, if user provides standard PBKDF2 iterations (e.g. 100000), it's invalid for N.
        # We'll use defaults if iterations is the PBKDF2 default.
        
        n_val = SCRYPT_DEFAULT_N
        if iterations != DEFAULT_ITERATIONS and iterations != ARGON2_DEFAULT_ITERATIONS:
             # Try to interpret iterations as N
             # Ensure power of 2
             if (iterations & (iterations - 1) == 0) and iterations > 1:
                 n_val = iterations
        
        kdf = Scrypt(
            salt=salt,
            length=key_length,
            n=n_val,
            r=SCRYPT_DEFAULT_R,
            p=SCRYPT_DEFAULT_P
        )
        return kdf.derive(password)

    kdf = PBKDF2HMAC(
        algorithm=get_digest_algorithm(digest_name),
        length=key_length,
        salt=salt,
        iterations=iterations,
        backend=default_backend()
    )
    return kdf.derive(password)

def _compute_siv_params(
    password: Union[str, bytes],
    data: bytes,
    context: Union[str, bytes],
    digest_name: str,
    iv_len: int,
    cipher_name: str = "",
    iterations: int = 0,
    kdf_name: str = DEFAULT_KDF
) -> Tuple[bytes, bytes]:
    """
    Computes deterministic Salt and IV using S2V-like construction:
    Hash(Password | Len(Context) | Context | Data)
    """
    if isinstance(context, str):
        context = context.encode('utf-8')
    if isinstance(password, str):
        pwd_bytes = password.encode('utf-8')
    else:
        pwd_bytes = password

    h = hashes.Hash(get_digest_algorithm(digest_name), backend=default_backend())
    # Canonicalize input to prevent concatenation attacks
    # Format: Hash( Len(Algo) || Algo || SEP || Len(Pwd) || Pwd || SEP || Len(Ctx) || Ctx || SEP || Data )
    # We use Length Prefixing which mathematically prevents collisions.
    # We also include a Null Byte (\x00) as a standard binary separator (Domain Separation).
    sep = b"\x00"
    
    # 1. Algo Params (Length prefixed)
    # Include algo info to prevent cross-protocol attacks if config changes.
    # We construct Domain Separation
    algo_params = f"{digest_name}:{cipher_name}:{iterations}:{kdf_name}".encode('utf-8')
    h.update(len(algo_params).to_bytes(4, byteorder='big'))
    h.update(algo_params)
    h.update(sep)

    # 2. Length prefix password (4 bytes big endian)
    h.update(len(pwd_bytes).to_bytes(4, byteorder='big'))
    h.update(pwd_bytes)
    h.update(sep)

    # 3. Length prefix context (4 bytes big endian)
    h.update(len(context).to_bytes(4, byteorder='big'))
    h.update(context)
    h.update(sep)

    h.update(data)

    digest_val = h.finalize()

    required_len = SALT_SIZE + iv_len
    if len(digest_val) < required_len:
         # Fallsack or expand? For now, assume standard digests are large enough (SHA256=32 bytes)
         # Salt(8) + AES-IV(16) = 24 bytes. SHA256 is fine.
         # For ChaCha (Nonce 12) = 20 bytes. Fine.
         # For GCM (Nonce 12) = 20 bytes. Fine.
         raise ValueError(f"Digest algorithm {digest_name} output ({len(digest_val)}) too short for needed SIV material ({required_len})")

    # Use first part for IV, next for Salt (arbitrary but consistent)
    iv = digest_val[:iv_len]
    salt = digest_val[iv_len : iv_len + SALT_SIZE]
    return salt, iv

def encrypt(
    data: bytes,
    password: str,
    salt: Optional[bytes] = None,
    iv: Optional[bytes] = None,
    iterations: int = DEFAULT_ITERATIONS,
    digest: str = DEFAULT_DIGEST,
    cipher_name: str = DEFAULT_CIPHER,
    deterministic: bool = False,
    context: Union[str, bytes] = b"",
    kdf: str = DEFAULT_KDF
) -> bytes:
    """
    Encrypt data.

    Args:
        data: Plaintext bytes.
        password: Password string.
        salt: Optional salt. If None and not deterministic, random salt is generated.
        iv: Optional IV. If None and not deterministic, random IV is generated.
        iterations: KDF iterations (Time Cost).
        digest: Hash algorithm for KDF.
        cipher_name: Cipher algorithm string.
        deterministic: If True, derives Salt and IV from data/password/context (SIV mode).
                       Ensures identical output for identical input.
        context: Additional context data for deterministic mode (e.g. filename) and AAD.
        kdf: KDF algorithm ('pbkdf2', 'argon2id').
    """
    algo_name, mode_val, key_len = _parse_cipher_name(cipher_name)

    # Argon2id logic
    if kdf == "argon2id" and iterations == DEFAULT_ITERATIONS:
        iterations = ARGON2_DEFAULT_ITERATIONS

    # Determine IV/Nonce size
    if algo_name == 'chacha20' and mode_val == 'poly1305':
        iv_len = NONCE_SIZE_CHACHA
    elif mode_val == 'gcm':
        iv_len = NONCE_SIZE_GCM
    else:
        iv_len = BLOCK_SIZE_AES

    # Handle SIV / Deterministic Mode
    if deterministic:
        if salt is not None or iv is not None:
            raise ValueError("Cannot provide explicit salt/iv when using deterministic mode.")
        salt, iv = _compute_siv_params(password, data, context, digest, iv_len, cipher_name, iterations, kdf_name=kdf)
    else:
        # Standard Randomized Mode
        if salt is None:
            salt = os.urandom(SALT_SIZE)
        if iv is None:
            iv = os.urandom(iv_len)

    # Validation
    if len(salt) != SALT_SIZE:
        raise ValueError(f"Salt must be {SALT_SIZE} bytes")
    if len(iv) != iv_len:
        raise ValueError(f"IV/Nonce must be {iv_len} bytes for {cipher_name}")

    # Derive Key
    key = derive_key(password, salt, iterations, digest, key_len, kdf_name=kdf)

    # Encryption Logic
    ciphertext = b""
    
    # Prepare AAD from context
    aad = context if isinstance(context, bytes) else context.encode('utf-8')

    if algo_name == 'chacha20' and mode_val == 'poly1305':
        # ChaCha20-Poly1305
        # Note: We use the one-shot API which appends tag to ciphertext usually
        cipher = ChaCha20Poly1305(key)
        # encrypt(nonce, data, associated_data) -> ciphertext + tag
        ciphertext_with_tag = cipher.encrypt(iv, data, aad)
        # Standard: Ciphertext || Tag
        # python cryptography returns Ciphertext || Tag
        ciphertext = ciphertext_with_tag

    elif mode_val == 'gcm':
        # AES-GCM
        cipher = AESGCM(key)
        ciphertext_with_tag = cipher.encrypt(iv, data, aad)
        ciphertext = ciphertext_with_tag

    else:
        # Classic Block Modes (CBC, CTR, etc)
        # Pad Data if Block Mode
        # Ensure mode_val is a class
        mode_cls = mode_val
        should_pad = mode_cls in [modes.CBC, modes.ECB]

        if should_pad:
            padder = padding.PKCS7(BLOCK_SIZE_AES * 8).padder()
            processed_data = padder.update(data) + padder.finalize()
        else:
            processed_data = data

        # Init Cipher
        if mode_cls == modes.ECB:
            mode_inst = mode_cls()
        elif mode_cls == modes.CTR:
            mode_inst = mode_cls(iv)
        else:
            mode_inst = mode_cls(iv)

        if algo_name == 'aes':
            algo_cls = algorithms.AES
        elif algo_name == 'sm4':
            algo_cls = algorithms.SM4
        elif algo_name == 'camellia':
            algo_cls = algorithms.Camellia
        else:
            raise ValueError(f"Unknown block cipher algorithm: {algo_name}")

        cipher = Cipher(algo_cls(key), mode_inst, backend=default_backend())
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(processed_data) + encryptor.finalize()

    # Construct Packet
    # Format: Salted__ + salt + IVed__ + iv + ciphertext (potentially with tag appended)
    packet = SALT_HEADER + salt + IV_HEADER + iv + ciphertext

    # Base64 Encode
    return base64.b64encode(packet)

def decrypt(
    data_b64: bytes,
    password: str,
    iterations: int = DEFAULT_ITERATIONS,
    digest: str = DEFAULT_DIGEST,
    cipher_name: str = DEFAULT_CIPHER,
    deterministic: bool = False,
    context: Union[str, bytes] = b"",
    kdf: str = DEFAULT_KDF
) -> bytes:
    """
    Decrypt data.

    Args:
        ...
        deterministic: If True, verifies that the Salt/IV embedded in the packet
                       matches the expected SIV for the decrypted content.
                       Provides content integrity verification.
        context: Context used during encryption (must match).
        kdf: KDF algorithm ('pbkdf2', 'argon2id').
    """
    algo_name, mode_val, key_len = _parse_cipher_name(cipher_name)

    # Argon2id logic
    if kdf == "argon2id" and iterations == DEFAULT_ITERATIONS:
        iterations = ARGON2_DEFAULT_ITERATIONS

    try:
        packet = base64.b64decode(data_b64, validate=True)
    except Exception as e:
        raise ValueError("Invalid Base64 input") from e

    # Parse Salt
    if not packet.startswith(SALT_HEADER):
        raise ValueError("Invalid format: Missing Salt header")
    offset = len(SALT_HEADER)
    salt = packet[offset : offset + SALT_SIZE]
    offset += SALT_SIZE

    # Determine needed IV length for parsing
    if algo_name == 'chacha20' and mode_val == 'poly1305':
        iv_len = NONCE_SIZE_CHACHA
    elif mode_val == 'gcm':
        iv_len = NONCE_SIZE_GCM
    else:
        iv_len = BLOCK_SIZE_AES

    # Parse IV
    if not packet[offset:].startswith(IV_HEADER):
        raise ValueError("Invalid format: Missing IV header")
    offset += len(IV_HEADER)
    iv = packet[offset : offset + iv_len]
    offset += iv_len

    ciphertext = packet[offset:]

    # Derive Key
    key = derive_key(password, salt, iterations, digest, key_len, kdf_name=kdf)

    # Decryption Logic
    plaintext = b""
    aad = context if isinstance(context, bytes) else context.encode('utf-8')

    if algo_name == 'chacha20' and mode_val == 'poly1305':
        cipher = ChaCha20Poly1305(key)
        # decrypt(nonce, data, associated_data)
        # Raises InvalidTag if tag verification fails
        try:
            plaintext = cipher.decrypt(iv, ciphertext, aad)
        except Exception:
            raise ValueError("Decryption failed (AEAD Tag Check Failed)")

    elif mode_val == 'gcm':
        cipher = AESGCM(key)
        try:
            plaintext = cipher.decrypt(iv, ciphertext, aad)
        except Exception:
            raise ValueError("Decryption failed (AEAD Tag Check Failed)")

    else:
        # Classic Block Modes
        mode_cls = mode_val

        # Init Cipher
        if mode_cls == modes.ECB:
            mode_inst = mode_cls()
        elif mode_cls == modes.CTR:
            mode_inst = mode_cls(iv)
        else:
            mode_inst = mode_cls(iv)

        if algo_name == 'aes':
            algo_cls = algorithms.AES
        elif algo_name == 'sm4':
            algo_cls = algorithms.SM4
        elif algo_name == 'camellia':
            algo_cls = algorithms.Camellia
        else:
            raise ValueError(f"Unknown block cipher algorithm: {algo_name}")

        cipher = Cipher(algo_cls(key), mode_inst, backend=default_backend())
        decryptor = cipher.decryptor()
        processed_data = decryptor.update(ciphertext) + decryptor.finalize()

        # Unpad
        should_pad = mode_cls in [modes.CBC, modes.ECB]

        if should_pad:
            unpadder = padding.PKCS7(BLOCK_SIZE_AES * 8).unpadder()
            try:
                plaintext = unpadder.update(processed_data) + unpadder.finalize()
            except ValueError:
                 raise ValueError("Decryption failed (Padding Error) - Wrong Password?")
        else:
            plaintext = processed_data

    # Post-Decryption SIV Verification
    if deterministic:
        expected_salt, expected_iv = _compute_siv_params(password, plaintext, context, digest, iv_len, cipher_name, iterations, kdf_name=kdf)
        if salt != expected_salt or iv != expected_iv:
            raise ValueError("Integrity Check Failed: Data may have been tampered with or parameters do not match content.")

    return plaintext
