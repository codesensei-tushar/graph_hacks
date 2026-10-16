"""GitHub webhook signing: ``X-Hub-Signature-256`` is ``sha256=<hex HMAC of the raw body>``."""

import hashlib
import hmac
from collections.abc import Mapping


def verify_github_signature(raw_body: bytes, headers: Mapping[str, str], webhook_secret: str) -> bool:
    signature = headers.get("x-hub-signature-256")
    if not signature or not webhook_secret:
        return False
    expected = "sha256=" + hmac.new(webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
