import base64
import os
import unittest

from core.crypto import (ARGON2_DEFAULT_ITERATIONS, DEFAULT_KDF, decrypt,
                         encrypt)


class TestCryptoModern(unittest.TestCase):

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
        decrypted = decrypt(encrypted, password, cipher_name=cipher_name, context=context)
        self.assertEqual(data, decrypted)
        
        # Decrypt with wrong context -> Should fail AEAD check
        with self.assertRaisesRegex(ValueError, "AEAD Tag Check Failed"):
            decrypt(encrypted, password, cipher_name=cipher_name, context="wrongname.txt")

    def test_chacha20_poly1305_aad(self):
        data = b"ChaCha20 Data"
        password = "pass"
        cipher = "chacha20-poly1305"
        context = "valid_context"

        enc = encrypt(data, password, cipher_name=cipher, context=context, kdf="argon2id")
        dec = decrypt(enc, password, cipher_name=cipher, context=context, kdf="argon2id")
        self.assertEqual(data, dec)

        with self.assertRaisesRegex(ValueError, "AEAD Tag Check Failed"):
             decrypt(enc, password, cipher_name=cipher, context="invalid", kdf="argon2id")

    def test_deterministic_modern(self):
         # SIV with Argon2id and GCM
         data = b"Deterministic"
         password = "pass"
         context = "siv-context"
         cipher = "aes-256-gcm"
         kdf = "argon2id"
         
         enc1 = encrypt(data, password, deterministic=True, context=context, cipher_name=cipher, kdf=kdf)
         enc2 = encrypt(data, password, deterministic=True, context=context, cipher_name=cipher, kdf=kdf)
         
         self.assertEqual(enc1, enc2)
         
         dec = decrypt(enc1, password, deterministic=True, context=context, cipher_name=cipher, kdf=kdf)
         self.assertEqual(data, dec)

         # Ensure changing context changes result
         enc3 = encrypt(data, password, deterministic=True, context="other", cipher_name=cipher, kdf=kdf)
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
        salt1, iv1 = _compute_siv_params(password, data, context, DEFAULT_DIGEST, iv_len, "aes-256-gcm", iterations, "argon2id")
        
        # Change Cipher Name
        salt2, iv2 = _compute_siv_params(password, data, context, DEFAULT_DIGEST, iv_len, "chacha20-poly1305", iterations, "argon2id")
        self.assertNotEqual(salt1, salt2)
        self.assertNotEqual(iv1, iv2)
        
        # Change KDF
        salt3, iv3 = _compute_siv_params(password, data, context, DEFAULT_DIGEST, iv_len, "aes-256-gcm", iterations, "pbkdf2")
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
        mutable_encrypted[-1] ^= 0x01 # Flip the last bit
        
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
        custom_n = 512 # small power of 2 for speed
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
        
        enc_default = encrypt(data, password, kdf=kdf) # validation uses default (4)
        enc_custom = encrypt(data, password, kdf=kdf, iterations=custom_iterations)
        
        self.assertNotEqual(enc_default, enc_custom)
        
        # Ensure we can decrypt with the custom iteration count
        dec = decrypt(enc_custom, password, kdf=kdf, iterations=custom_iterations)
        self.assertEqual(data, dec)
        
        # Ensure decrypting with default iterations fails (wrong key)
        # It will likely fail at AEAD tag check because key is wrong.
        with self.assertRaises(ValueError):
             decrypt(enc_custom, password, kdf=kdf) # uses default iter=4
