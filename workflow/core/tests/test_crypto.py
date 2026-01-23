import base64
import os
import unittest

from core import decrypt, encrypt
from core.crypto import (
    ARGON2_DEFAULT_ITERATIONS,
    DEFAULT_KDF,
    IV_HEADER,
    SALT_HEADER,
    decrypt,
    encrypt,
)


class TestCrypto(unittest.TestCase):
    def test_roundtrip_defaults(self):
        data = b"Secret Message"
        password = "strongpassword"

        encrypted = encrypt(data, password)
        # Verify structure
        decoded = base64.b64decode(encrypted)
        self.assertTrue(decoded.startswith(SALT_HEADER))
        self.assertIn(IV_HEADER, decoded)

        decrypted = decrypt(encrypted, password)
        self.assertEqual(data, decrypted)

    def test_roundtrip_custom_params(self):
        data = b"Another Secret"
        password = "pass"
        iterations = 1000
        digest = "sha512"

        encrypted = encrypt(data, password, iterations=iterations, digest=digest)
        decrypted = decrypt(encrypted, password, iterations=iterations, digest=digest)
        self.assertEqual(data, decrypted)

    def test_explicit_salt_iv(self):
        data = b"Deterministic Secret"
        password = "pass"
        salt = os.urandom(8)
        iv = os.urandom(16)

        encrypted = encrypt(data, password, salt=salt, iv=iv)

        # Verify salt and iv are embedded correctly
        decoded = base64.b64decode(encrypted)

        salt_offset = len(SALT_HEADER)
        extracted_salt = decoded[salt_offset : salt_offset + 8]
        self.assertEqual(salt, extracted_salt)

        iv_offset = salt_offset + 8 + len(IV_HEADER)
        extracted_iv = decoded[iv_offset : iv_offset + 16]
        self.assertEqual(iv, extracted_iv)

        decrypted = decrypt(encrypted, password)
        self.assertEqual(data, decrypted)

    def test_decrypt_invalid_inputs(self):
        password = "pass"
        # Not base64
        with self.assertRaises(ValueError):
            decrypt(b"!!!", password)

        # Missing headers
        data = b"Some data"
        b64 = base64.b64encode(data)
        with self.assertRaises(ValueError):
            decrypt(b64, password)

    def test_encrypt_large_data(self):
        data = os.urandom(1024 * 1024)  # 1MB
        password = "pass"
        encrypted = encrypt(data, password)
        decrypted = decrypt(encrypted, password)
        self.assertEqual(data, decrypted)

    def test_different_modes(self):
        data = b"Some data"
        password = "pass"

        # Helper to test roundtrip
        def check(cipher_name):
            enc = encrypt(data, password, cipher_name=cipher_name)
            dec = decrypt(enc, password, cipher_name=cipher_name)
            self.assertEqual(data, dec, f"Failed for {cipher_name}")

        check("aes-128-cbc")
        check("aes-192-cfb")
        check("aes-256-ctr")
        check("aes-256-ecb")
        check("aes-256-ofb")

    def test_digests(self):
        data = b"Digest test"
        password = "pass"

        def check(digest):
            enc = encrypt(data, password, digest=digest)
            dec = decrypt(enc, password, digest=digest)
            self.assertEqual(data, dec, f"Failed for digest {digest}")

        check("sha224")
        check("sha384")
        check("sha512")

    def test_deterministic_basic(self):
        """Test simple deterministic encryption"""
        data = b"DeterministicContent"
        password = "siv_pass"

        # 1. Encrypting twice produces identical output
        enc1 = encrypt(data, password, deterministic=True)
        enc2 = encrypt(data, password, deterministic=True)
        self.assertEqual(enc1, enc2)

        # 2. Decrypt works
        dec1 = decrypt(enc1, password, deterministic=True)
        self.assertEqual(dec1, data)

    def test_deterministic_context(self):
        """Test that context changes the ciphertext"""
        data = b"SameContent"
        password = "pass"

        enc1 = encrypt(data, password, deterministic=True, context="file1.txt")
        enc2 = encrypt(data, password, deterministic=True, context="file2.txt")

        # Ciphertexts must differ
        self.assertNotEqual(enc1, enc2)

        # Correct context decrypts
        dec1 = decrypt(enc1, password, deterministic=True, context="file1.txt")
        self.assertEqual(dec1, data)

        # Wrong context fails integrity check (IV mismatch)
        with self.assertRaises(ValueError) as cm:
            decrypt(enc1, password, deterministic=True, context="file2.txt")
        self.assertIn("Integrity Check Failed", str(cm.exception))

    def test_deterministic_integrity(self):
        """Test tampering validation"""
        data = b"Important Data"
        password = "pass"

        enc = encrypt(data, password, deterministic=True)
        decoded = base64.b64decode(enc)

        # Tamper with the last byte of ciphertext (assuming it's at the end)
        tampered_decoded = decoded[:-1] + bytes([decoded[-1] ^ 0xFF])
        tampered_enc = base64.b64encode(tampered_decoded)

        # Should fail
        with self.assertRaises(ValueError):
            decrypt(tampered_enc, password, deterministic=True)

    def test_conflict_flags(self):
        """Cannot use deterministic with explicit salt/iv"""
        with self.assertRaises(ValueError):
            encrypt(b"data", "pass", deterministic=True, salt=os.urandom(8))

    def test_randomness(self):
        """Encrypting same data twice with defaults should produce different outputs due to random Salt/IV"""
        data = b"Sensitive"
        pwd = "pwd"
        enc1 = encrypt(data, pwd)
        enc2 = encrypt(data, pwd)
        self.assertNotEqual(enc1, enc2)

        # Also verify decoding works for both
        self.assertEqual(decrypt(enc1, pwd), data)
        self.assertEqual(decrypt(enc2, pwd), data)

    def test_wrong_password(self):
        """Verifies behavior with wrong password"""
        data = b"Secret"
        pwd = "correct_password"
        wrong = "wrong_password"

        # 1. AEAD (GCM) - MUST fail
        enc_gcm = encrypt(data, pwd, cipher_name="aes-256-gcm")
        with self.assertRaises(ValueError) as cm:
            decrypt(enc_gcm, wrong, cipher_name="aes-256-gcm")
        self.assertIn("Tag Check Failed", str(cm.exception))

        # 2. CBC - Likely fails Padding
        enc_cbc = encrypt(data, pwd, cipher_name="aes-256-cbc")
        with self.assertRaises(ValueError) as cm:
            decrypt(enc_cbc, wrong, cipher_name="aes-256-cbc")
        self.assertIn("Padding Error", str(cm.exception))

        # 3. CTR - Returns garbage (no integrity check)
        enc_ctr = encrypt(data, pwd, cipher_name="aes-256-ctr")
        dec_ctr = decrypt(enc_ctr, wrong, cipher_name="aes-256-ctr")
        self.assertNotEqual(dec_ctr, data)  # Garbage
        self.assertEqual(len(dec_ctr), len(data))  # Length preserved

    def test_chacha20_tampering(self):
        """Verifies ChaCha20-Poly1305 integrity check"""
        data = b"ChaChaSecret"
        pwd = "pass"
        cipher = "chacha20-poly1305"

        enc = encrypt(data, pwd, cipher_name=cipher)

        # Tamper
        raw = base64.b64decode(enc)
        # Flip bit in ciphertext region (end of string)
        # We need to make sure we are not flipping IV or Salt header, but the actual encrypted data
        tampered_raw = raw[:-1] + bytes([raw[-1] ^ 0xFF])
        tampered_enc = base64.b64encode(tampered_raw)

        with self.assertRaises(ValueError) as cm:
            decrypt(tampered_enc, pwd, cipher_name=cipher)
        self.assertIn("Tag Check Failed", str(cm.exception))

    def test_invalid_params_length(self):
        """Verifies enforcement of Salt/IV lengths"""
        data = b"msg"
        pwd = "pass"

        # Wrong Salt length (expect 8)
        with self.assertRaisesRegex(ValueError, "Salt must be 8 bytes"):
            encrypt(data, pwd, salt=os.urandom(7))

        # Wrong IV length for AES (expect 16)
        with self.assertRaisesRegex(ValueError, "IV/Nonce must be 16 bytes"):
            encrypt(data, pwd, iv=os.urandom(12), cipher_name="aes-256-cbc")

        # Wrong IV length for GCM (expect 12)
        with self.assertRaisesRegex(ValueError, "IV/Nonce must be 12 bytes"):
            encrypt(data, pwd, iv=os.urandom(16), cipher_name="aes-256-gcm")

    def test_aead_modes(self):
        data = b"AEAD Secret"
        password = "pass"

        # Test AES-256-GCM
        cipher = "aes-256-gcm"
        enc = encrypt(data, password, cipher_name=cipher)
        dec = decrypt(enc, password, cipher_name=cipher)
        self.assertEqual(data, dec)

        # Tamper Check for GCM
        raw = base64.b64decode(enc)
        # Flip a bit in the ciphertext part (last byte)
        tampered_raw = raw[:-1] + bytes([raw[-1] ^ 0xFF])
        tampered_enc = base64.b64encode(tampered_raw)

        with self.assertRaises(ValueError) as cm:
            decrypt(tampered_enc, password, cipher_name=cipher)
        self.assertIn("AEAD Tag Check Failed", str(cm.exception))

        # Test ChaCha20-Poly1305
        cipher = "chacha20-poly1305"
        enc = encrypt(data, password, cipher_name=cipher)
        dec = decrypt(enc, password, cipher_name=cipher)
        self.assertEqual(data, dec)

    def test_unsupported_ops(self):
        # Invalid algo
        with self.assertRaises(ValueError):
            encrypt(b"", "p", cipher_name="des-128-cbc")

        # Invalid bits
        with self.assertRaises(ValueError):
            encrypt(b"", "p", cipher_name="aes-55-cbc")

        # Invalid digest
        with self.assertRaises(ValueError):
            encrypt(b"", "p", digest="sha999")

    def test_sm4_encryption(self):
        password = "testpassword"
        data = b"Hello SM4 World"

        # Test SM4 CBC
        encrypted = encrypt(data, password, cipher_name="sm4-128-cbc")
        decrypted = decrypt(encrypted, password, cipher_name="sm4-128-cbc")
        self.assertEqual(data, decrypted)

    def test_camellia_encryption(self):
        password = "testpassword"
        data = b"Hello Camellia World"

        # Test Camellia CBC
        encrypted = encrypt(data, password, cipher_name="camellia-128-cbc")
        decrypted = decrypt(encrypted, password, cipher_name="camellia-128-cbc")
        self.assertEqual(data, decrypted)

    def test_blake2_digest(self):
        password = "testpassword"
        data = b"Hello BLAKE2 World"

        # Test using BLAKE2b for KDF (and SIV if we used deterministic)
        encrypted = encrypt(data, password, digest="blake2b")
        decrypted = decrypt(encrypted, password, digest="blake2b")
        self.assertEqual(data, decrypted)

    def test_blake2s_deterministic(self):
        password = "test"
        data = b"Deterministic BLAKE2s"
        context = "ctx"

        # Encrypt deterministically using BLAKE2s
        enc1 = encrypt(
            data, password, digest="blake2s", deterministic=True, context=context
        )
        enc2 = encrypt(
            data, password, digest="blake2s", deterministic=True, context=context
        )
        self.assertEqual(enc1, enc2)

        dec = decrypt(
            enc1, password, digest="blake2s", deterministic=True, context=context
        )
        self.assertEqual(data, dec)

    def test_argon2id_kdf(self):
        data = b"Argon2id Secret"
        password = "modernpassword"

        # Encrypt with argon2id
        encrypted = encrypt(data, password, kdf="argon2id")

        # Decrypt with argon2id
        decrypted = decrypt(encrypted, password, kdf="argon2id")
        self.assertEqual(data, decrypted)

        # Ensure defaults applied correctly (iterations didn't default to PBKDF2's high count)
        # We can't easily inspect the internal derivation without mocking,
        # but if it finished reasonably fast, it's good.

    def test_kdf_mismatch(self):
        data = b"Secret"
        password = "pass"
        encrypted = encrypt(data, password, kdf="argon2id")

        # Trying to decrypt with pbkdf2 should fail (wrong key derived)
        # Ideally it yields garbage, which AEAD tag check detects and rejects.
        try:
            decrypt(encrypted, password, kdf="pbkdf2")
        except ValueError as e:
            # Expected "Decryption failed (AEAD Tag Check Failed)" or similar
            # Or if classic mode, padding error.
            pass
        else:
            # Check if it coincidentally worked (unlikely) or didn't raise
            # Actually for AEAD it SHOULD fail.
            self.fail("Should have failed due to wrong KDF")

    def test_aad_context_binding(self):
        # Test that context is authenticated in AEAD modes
        data = b"Context bound data"
        password = "pass"
        context = "filename.txt"

        # Use a AEAD cipher explicitly or rely on default (aes-256-cbc is default legacy?)
        # Default cipher is AES-256-CBC ??
        # Wait, DEFAULT_CIPHER = "aes-256-cbc"
        # CBC is NOT AEAD. So context binding via AAD won't work for default cipher.
        # But deterministic mode uses context for SIV.

        # Let's test AEAD explicitly
        cipher_name = "aes-256-gcm"

        encrypted = encrypt(data, password, cipher_name=cipher_name, context=context)

        # Decrypt with correct context
        decrypted = decrypt(
            encrypted, password, cipher_name=cipher_name, context=context
        )
        self.assertEqual(data, decrypted)

        # Decrypt with wrong context -> Should fail AEAD check
        with self.assertRaisesRegex(ValueError, "AEAD Tag Check Failed"):
            decrypt(
                encrypted, password, cipher_name=cipher_name, context="wrongname.txt"
            )

    def test_chacha20_poly1305_aad(self):
        data = b"ChaCha20 Data"
        password = "pass"
        cipher = "chacha20-poly1305"
        context = "valid_context"

        enc = encrypt(
            data, password, cipher_name=cipher, context=context, kdf="argon2id"
        )
        dec = decrypt(
            enc, password, cipher_name=cipher, context=context, kdf="argon2id"
        )
        self.assertEqual(data, dec)

        with self.assertRaisesRegex(ValueError, "AEAD Tag Check Failed"):
            decrypt(
                enc, password, cipher_name=cipher, context="invalid", kdf="argon2id"
            )

    def test_deterministic_modern(self):
        # SIV with Argon2id and GCM
        data = b"Deterministic"
        password = "pass"
        context = "siv-context"
        cipher = "aes-256-gcm"
        kdf = "argon2id"

        enc1 = encrypt(
            data,
            password,
            deterministic=True,
            context=context,
            cipher_name=cipher,
            kdf=kdf,
        )
        enc2 = encrypt(
            data,
            password,
            deterministic=True,
            context=context,
            cipher_name=cipher,
            kdf=kdf,
        )

        self.assertEqual(enc1, enc2)

        dec = decrypt(
            enc1,
            password,
            deterministic=True,
            context=context,
            cipher_name=cipher,
            kdf=kdf,
        )
        self.assertEqual(data, dec)

        # Ensure changing context changes result
        enc3 = encrypt(
            data,
            password,
            deterministic=True,
            context="other",
            cipher_name=cipher,
            kdf=kdf,
        )
        self.assertNotEqual(enc1, enc3)

    def test_domain_separation(self):
        """
        Verify that changing any algorithm parameter (cipher, kdf, etc) results in
        different SIV (Salt/IV) derivation, ensuring proper domain separation.
        This prevents cross-protocol attacks or confusion.
        """
        from core.crypto import DEFAULT_DIGEST, _compute_siv_params

        password = "pass"
        data = b"data"
        context = b"ctx"
        iv_len = 12
        iterations = 1000

        # Base
        salt1, iv1 = _compute_siv_params(
            password,
            data,
            context,
            DEFAULT_DIGEST,
            iv_len,
            "aes-256-gcm",
            iterations,
            "argon2id",
        )

        # Change Cipher Name
        salt2, iv2 = _compute_siv_params(
            password,
            data,
            context,
            DEFAULT_DIGEST,
            iv_len,
            "chacha20-poly1305",
            iterations,
            "argon2id",
        )
        self.assertNotEqual(salt1, salt2)
        self.assertNotEqual(iv1, iv2)

        # Change KDF
        salt3, iv3 = _compute_siv_params(
            password,
            data,
            context,
            DEFAULT_DIGEST,
            iv_len,
            "aes-256-gcm",
            iterations,
            "pbkdf2",
        )
        self.assertNotEqual(salt1, salt3)
        self.assertNotEqual(iv1, iv3)

    def test_tampered_ciphertext(self):
        """
        Demonstrate authentication failure when the ciphertext itself is modified.
        This confirms that AEAD (GCM/Poly1305) protects data integrity.
        """
        data = b"Sensitive Data"
        password = "pass"
        cipher = "chacha20-poly1305"
        kdf = "argon2id"

        # 1. Encrypt
        encrypted_b64 = encrypt(data, password, cipher_name=cipher, kdf=kdf)
        encrypted_bytes = base64.b64decode(encrypted_b64)

        # 2. Tamper with the last byte (likely part of the tag or ciphertext)
        # Convert to mutable bytearray
        mutable_encrypted = bytearray(encrypted_bytes)
        mutable_encrypted[-1] ^= 0x01  # Flip the last bit

        tampered_b64 = base64.b64encode(mutable_encrypted)

        # 3. Decrypt should fail
        with self.assertRaisesRegex(ValueError, "AEAD Tag Check Failed"):
            decrypt(tampered_b64, password, cipher_name=cipher, kdf=kdf)

    def test_scrypt_kdf(self):
        """Verify Scrypt KDF works."""
        data = b"Scrypt Data"
        password = "pass"
        kdf = "scrypt"

        enc = encrypt(data, password, kdf=kdf)
        dec = decrypt(enc, password, kdf=kdf)
        self.assertEqual(data, dec)

        # Test custom N (iterations)
        custom_n = 512  # small power of 2 for speed
        enc_cust = encrypt(data, password, kdf=kdf, iterations=custom_n)
        self.assertNotEqual(enc, enc_cust)
        dec_cust = decrypt(enc_cust, password, kdf=kdf, iterations=custom_n)
        self.assertEqual(data, dec_cust)

    def test_argon2id_custom_iterations(self):
        """
        Verify that user can increase Argon2id iterations (Time Cost) beyond the default.
        """
        data = b"Stronger Data"
        password = "pass"
        kdf = "argon2id"
        custom_iterations = 3

        # Encrypt with custom iterations
        # We need to spy on derive_key or inspect logic, but since we can't easily spy
        # without mocking the module internal import, we can verify via Domain Separation.
        # Encryption with default should differ from iter=3.

        enc_default = encrypt(data, password, kdf=kdf)  # validation uses default (4)
        enc_custom = encrypt(data, password, kdf=kdf, iterations=custom_iterations)

        self.assertNotEqual(enc_default, enc_custom)

        # Ensure we can decrypt with the custom iteration count
        dec = decrypt(enc_custom, password, kdf=kdf, iterations=custom_iterations)
        self.assertEqual(data, dec)

        # Ensure decrypting with default iterations fails (wrong key)
        # It will likely fail at AEAD tag check because key is wrong.
        with self.assertRaises(ValueError):
            decrypt(enc_custom, password, kdf=kdf)  # uses default iter=4
