"""CSRF for the console: every mutation is JSON, sent from our own origin, with the session's token.

Three checks, any one of which stops a cross-site form or fetch:
- ``Origin`` must equal PUBLIC_URL (the request's own origin when PUBLIC_URL is unset).
- ``X-CSRF-Token`` must equal the token stored with the session (a custom header also
  forces a CORS preflight, which this server never answers).
- A body must be ``application/json``, which an HTML form cannot send.
"""

import secrets

from fastapi import HTTPException, Request

from src.core.config import settings

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
HEADER = "x-csrf-token"


def same(a: str, b: str) -> bool:
    """Constant-time string equality. Compares UTF-8 bytes: ``compare_digest`` raises on
    non-ASCII ``str``, and header values can carry any latin-1 character."""
    return secrets.compare_digest(a.encode(), b.encode())


def new_token() -> str:
    return secrets.token_urlsafe(32)


def expected_origin(request: Request) -> str:
    return settings.public_url or str(request.base_url).rstrip("/")


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin or origin.rstrip("/") != expected_origin(request):
        raise HTTPException(status_code=403, detail="bad_origin")


def check(request: Request, session_token: str) -> None:
    """Raises 403/415 unless the request is a same-origin JSON mutation carrying the token."""
    if request.method in SAFE_METHODS:
        return
    check_origin(request)
    presented = request.headers.get(HEADER, "")
    if not presented or not session_token or not same(presented, session_token):
        raise HTTPException(status_code=403, detail="bad_csrf_token")
    content_type = request.headers.get("content-type", "")
    has_body = request.headers.get("content-length", "0") not in ("", "0") or "transfer-encoding" in request.headers
    if (content_type or has_body) and content_type.split(";")[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="json_required")
