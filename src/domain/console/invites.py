"""Invite codes for org creation during the beta: ``(:InviteCode {code_hash, used_by, expires_at})``.

The operator mints a code with BRAIN_ADMIN_TOKEN (``POST /ops/invites``) and hands it
over. Only its SHA-256 is stored. A code works once and before it expires; if org
creation fails after the code was taken, ``release`` gives it back.
"""

import hashlib
import secrets
import time

from src.domain.console import contracts

DEFAULT_TTL_DAYS = 14
MAX_TTL_DAYS = 90
PREFIX = "brain-"
MAX_CODE_LENGTH = 128


def _hash(code: str) -> str:
    return hashlib.sha256(code.strip().encode()).hexdigest()


def mint(ttl_days: int = DEFAULT_TTL_DAYS, created_by: str = "operator") -> tuple[str, int]:
    """Returns ``(code, expires_at_ms)``. The code is shown once and never stored."""
    ttl_days = max(1, min(int(ttl_days), MAX_TTL_DAYS))
    code = PREFIX + secrets.token_urlsafe(18)
    now = int(time.time() * 1000)
    expires_at = now + ttl_days * 24 * 60 * 60 * 1000
    contracts.control_graph().query(
        "CREATE (:InviteCode {code_hash: $hash, used_by: null, expires_at: $expires, created_by: $by, created_at: $now})",
        {"hash": _hash(code), "expires": expires_at, "by": created_by, "now": now},
    )
    return code, expires_at


def consume(code: str, used_by: str) -> bool:
    """Marks the code used by ``used_by``. False for an unknown, used or expired code.

    One query, and FalkorDB runs a graph's writes one at a time, so two people
    racing for the same code can't both get it.
    """
    code = (code or "").strip()
    if not code or len(code) > MAX_CODE_LENGTH:
        return False
    rows = contracts.control_graph().query(
        """
        MATCH (i:InviteCode {code_hash: $hash})
        WHERE i.used_by IS NULL AND i.expires_at > $now
        SET i.used_by = $by, i.used_at = $now
        RETURN count(i)
        """,
        {"hash": _hash(code), "now": int(time.time() * 1000), "by": used_by},
    ).result_set
    return bool(rows and rows[0][0])


def release(code: str, used_by: str) -> None:
    """Gives a code back after the org it was taken for could not be created."""
    contracts.control_graph().query(
        "MATCH (i:InviteCode {code_hash: $hash, used_by: $by}) SET i.used_by = null, i.used_at = null",
        {"hash": _hash(code), "by": used_by},
    )
