"""Slack request signing. Port of company-brain ``brain/slack/verify.ts``."""

import hashlib
import hmac
import time
from collections.abc import Mapping

MAX_SKEW_SECONDS = 60 * 5


def verify_slack_signature(
    raw_body: bytes,
    headers: Mapping[str, str],
    signing_secret: str,
    now_seconds: int | None = None,
) -> bool:
    signature = headers.get("x-slack-signature")
    timestamp = headers.get("x-slack-request-timestamp")
    if not signature or not timestamp or not signing_secret:
        return False

    try:
        ts = int(timestamp)
    except ValueError:
        return False
    now = int(time.time()) if now_seconds is None else now_seconds
    if abs(now - ts) > MAX_SKEW_SECONDS:
        return False

    base = f"v0:{timestamp}:{raw_body.decode('utf-8')}".encode()
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def is_slack_retry(headers: Mapping[str, str]) -> bool:
    return headers.get("x-slack-retry-num") is not None
