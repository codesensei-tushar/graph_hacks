import hashlib
import hmac
import unittest

from src.integrations.github.verify import verify_github_signature

SECRET = "webhook-secret"
BODY = b'{"action":"opened"}'


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class GitHubSignatureTest(unittest.TestCase):
    def test_valid_signature(self):
        self.assertTrue(verify_github_signature(BODY, {"x-hub-signature-256": _sign(BODY)}, SECRET))

    def test_wrong_secret(self):
        self.assertFalse(verify_github_signature(BODY, {"x-hub-signature-256": _sign(BODY, "other")}, SECRET))

    def test_tampered_body(self):
        self.assertFalse(verify_github_signature(b'{"action":"closed"}', {"x-hub-signature-256": _sign(BODY)}, SECRET))

    def test_missing_header_or_secret(self):
        self.assertFalse(verify_github_signature(BODY, {}, SECRET))
        self.assertFalse(verify_github_signature(BODY, {"x-hub-signature-256": _sign(BODY)}, ""))


if __name__ == "__main__":
    unittest.main()
