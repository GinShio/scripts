import unittest
import os
import sys

# Adjust path to include workflow root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from workflow.core import crypto

class TestCryptoExtra(unittest.TestCase):
    def test_sm4_encryption(self):
        password = "testpassword"
        data = b"Hello SM4 World"

        # Test SM4 CBC
        encrypted = crypto.encrypt(data, password, cipher_name="sm4-128-cbc")
        decrypted = crypto.decrypt(encrypted, password, cipher_name="sm4-128-cbc")
        self.assertEqual(data, decrypted)

    def test_camellia_encryption(self):
        password = "testpassword"
        data = b"Hello Camellia World"

        # Test Camellia CBC
        encrypted = crypto.encrypt(data, password, cipher_name="camellia-128-cbc")
        decrypted = crypto.decrypt(encrypted, password, cipher_name="camellia-128-cbc")
        self.assertEqual(data, decrypted)

    def test_blake2_digest(self):
        password = "testpassword"
        data = b"Hello BLAKE2 World"

        # Test using BLAKE2b for KDF (and SIV if we used deterministic)
        encrypted = crypto.encrypt(data, password, digest="blake2b")
        decrypted = crypto.decrypt(encrypted, password, digest="blake2b")
        self.assertEqual(data, decrypted)

    def test_blake2s_deterministic(self):
        password = "test"
        data = b"Deterministic BLAKE2s"
        context = "ctx"

        # Encrypt deterministically using BLAKE2s
        enc1 = crypto.encrypt(data, password, digest="blake2s", deterministic=True, context=context)
        enc2 = crypto.encrypt(data, password, digest="blake2s", deterministic=True, context=context)
        self.assertEqual(enc1, enc2)

        dec = crypto.decrypt(enc1, password, digest="blake2s", deterministic=True, context=context)
        self.assertEqual(data, dec)

if __name__ == '__main__':
    unittest.main()
