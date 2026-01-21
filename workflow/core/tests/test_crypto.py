import base64
import os
import unittest

from core import decrypt, encrypt
from core.crypto import IV_HEADER, SALT_HEADER


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
        data = os.urandom(1024 * 1024) # 1MB
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
        self.assertNotEqual(dec_ctr, data) # Garbage
        self.assertEqual(len(dec_ctr), len(data)) # Length preserved

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


if __name__ == '__main__':
    unittest.main()
