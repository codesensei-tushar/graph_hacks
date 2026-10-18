"""MultiFernet: the first key encrypts, every key decrypts, and rewrap moves values onto the first key."""

import unittest

import tests  # noqa: F401  pins the test graph and owner before src loads

from cryptography.fernet import Fernet  # noqa: E402

from src.core.config import settings  # noqa: E402
from src.domain.slack import token_crypto  # noqa: E402


class TokenKeysTest(unittest.TestCase):
    def setUp(self):
        self.saved = (settings.token_encryption_keys, settings.token_encryption_key)
        self.old, self.new = Fernet.generate_key().decode(), Fernet.generate_key().decode()

    def tearDown(self):
        object.__setattr__(settings, "token_encryption_keys", self.saved[0])
        object.__setattr__(settings, "token_encryption_key", self.saved[1])

    def keys(self, keys=(), single=""):
        object.__setattr__(settings, "token_encryption_keys", tuple(keys))
        object.__setattr__(settings, "token_encryption_key", single)

    def test_the_single_legacy_key_still_works_and_decrypts_after_rotation(self):
        self.keys(single=self.old)
        stored = token_crypto.encrypt_token("xoxb-1")
        self.keys([self.new], single=self.old)
        self.assertEqual(token_crypto.decrypt_token(stored), "xoxb-1")
        rewrapped = token_crypto.rewrap_token(stored)
        self.assertIsNotNone(rewrapped)
        self.assertIsNone(token_crypto.rewrap_token(rewrapped))  # already on the first key
        self.keys([self.new])
        self.assertEqual(token_crypto.decrypt_token(rewrapped), "xoxb-1")
        with self.assertRaises(Exception):
            token_crypto.decrypt_token(stored)

    def test_plaintext_legacy_values_are_read_for_slack_and_encrypted_by_rewrap(self):
        self.keys()
        stored = token_crypto.encrypt_token("xoxb-2")
        self.assertTrue(stored.startswith("plain:"))
        self.assertIsNone(token_crypto.rewrap_token(stored))  # no key yet: nothing to do
        self.keys([self.new])
        self.assertEqual(token_crypto.decrypt_token(stored), "xoxb-2")
        rewrapped = token_crypto.rewrap_token(stored)
        self.assertTrue(rewrapped.startswith("fernet:"))
        self.assertEqual(token_crypto.decrypt_token(rewrapped, allow_plaintext=False), "xoxb-2")
        with self.assertRaises(ValueError):
            token_crypto.decrypt_token(stored, allow_plaintext=False)

    def test_connector_secrets_are_never_stored_in_plaintext(self):
        self.keys()
        with self.assertRaises(RuntimeError):
            token_crypto.encrypt_token("secret", allow_plaintext=False)
        self.keys([self.new, self.old])
        self.assertEqual(token_crypto.current_key_id(), token_crypto.current_key_id())
        self.assertEqual(len(token_crypto.current_key_id()), 12)


if __name__ == "__main__":
    unittest.main()
