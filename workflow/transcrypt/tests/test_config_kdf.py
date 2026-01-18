import unittest
import os
from unittest.mock import patch
from workflow.transcrypt.src.actions import _get_kdf
from workflow.core import crypto

class TestConfigKDF(unittest.TestCase):
    def test_default_kdf(self):
        # Ensure clean state
        with patch.dict(os.environ, {}, clear=True):
            with patch('workflow.transcrypt.src.actions.get_git_config', return_value=None):
                self.assertEqual(_get_kdf(), crypto.DEFAULT_KDF)

    def test_env_kdf(self):
        with patch.dict(os.environ, {"TRANSCRYPT_KDF": "argon2id"}):
             self.assertEqual(_get_kdf(), "argon2id")
             
    def test_env_context_kdf(self):
         with patch.dict(os.environ, {"TRANSCRYPT_PROD_KDF": "scrypt"}):
             self.assertEqual(_get_kdf("prod"), "scrypt")

    def test_git_config_kdf(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch('workflow.transcrypt.src.actions.get_git_config', return_value="pbkdf2"):
                self.assertEqual(_get_kdf(), "pbkdf2")

if __name__ == '__main__':
    unittest.main()
