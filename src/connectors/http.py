"""A small JSON client for connectors, on urllib like ``src/integrations/github/client.py``.

It retries what is worth retrying and gives up in words ``sync.py`` understands:

- 429 or 503 with a short ``Retry-After`` (up to ``MAX_INLINE_WAIT`` seconds): wait and retry;
  a longer one raises ``RetryLater``, so the scheduler thread is never held for long.
- other 5xx and network errors: retried after 1 s, then 2 s, then ``ConnectorError``.
- 401: ``CredentialsRejected``. Any other 4xx, and any redirect: ``HttpError`` with the status.
- a ``Retry-After`` is never honoured past ``base.MAX_RETRY_AFTER_SECONDS``.

Credentials travel only in the headers given to the constructor, only to the base URL's
origin (a pagination link to another host is refused), and never into a log line or an
error message.
"""

import base64
import email.utils
import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from src.connectors.base import ConnectorError, CredentialsRejected, RetryLater, clamp_wait

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20
MAX_ATTEMPTS = 3
MAX_INLINE_WAIT = 5.0
# When a 429 names no wait.
DEFAULT_RETRY_AFTER = 60.0
_RETRY_STATUSES = frozenset({500, 502, 503, 504})


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would resend the Authorization header to wherever it
    points, any host, even over http. The 3xx comes back as an ``HttpError`` instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Seams for tests.
_urlopen = urllib.request.build_opener(_NoRedirects).open
_sleep = time.sleep


class HttpError(ConnectorError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def basic_auth(user: str, token: str) -> dict[str, str]:
    """Jira Cloud: the account's email and an API token."""
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()}


def bearer(token: str) -> dict[str, str]:
    """HubSpot private apps, and most others."""
    return {"Authorization": f"Bearer {token}"}


def retry_after_seconds(value: str | None) -> float | None:
    """``Retry-After`` as seconds, at most ``base.MAX_RETRY_AFTER_SECONDS``: a number, or an HTTP date.
    None when absent or unreadable."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = email.utils.parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError):
            return None
    return clamp_wait(seconds)


def _origin(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


class JsonClient:
    def __init__(self, base_url: str, headers: dict[str, str] | None = None, timeout: float = TIMEOUT_SECONDS):
        self.base_url = base_url.rstrip("/")
        self._headers = {"Accept": "application/json", "User-Agent": "company-brain", **(headers or {})}
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"JsonClient({self.base_url!r})"

    def url(self, path: str, params: dict | None = None) -> str:
        """``path`` under the base URL, or a full URL on the same origin (a "next" link)."""
        if path.startswith(("http://", "https://")):
            if _origin(path) != _origin(self.base_url):
                raise ConnectorError(f"refusing to send credentials to another host: {_origin(path)[1]}")
            url = path
        else:
            url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        return url

    def get(self, path: str, params: dict | None = None) -> object:
        return self.request("GET", path, params=params)[0]

    def post(self, path: str, body: object = None, params: dict | None = None) -> object:
        return self.request("POST", path, params=params, body=body)[0]

    def request(self, method: str, path: str, params: dict | None = None, body: object = None) -> tuple[object, dict]:
        """The parsed JSON body (None when empty) and the response headers, lowercased."""
        url = self.url(path, params)
        headers = dict(self._headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        where = urllib.parse.urlsplit(url).path
        for attempt in range(1, MAX_ATTEMPTS + 1):
            last = attempt == MAX_ATTEMPTS
            try:
                with _urlopen(urllib.request.Request(url, data=data, method=method, headers=headers), timeout=self.timeout) as response:
                    raw = response.read()
                    return (json.loads(raw) if raw else None), {key.lower(): value for key, value in response.headers.items()}
            except urllib.error.HTTPError as error:
                status = error.code
                wait = retry_after_seconds(error.headers.get("Retry-After") if error.headers else None)
                detail = error.read().decode(errors="replace")[:200] if error.fp else ""
                if status == 401:
                    raise CredentialsRejected(f"{method} {where} answered 401") from None
                if status == 429 or (status == 503 and wait is not None):
                    wait = DEFAULT_RETRY_AFTER if wait is None else wait
                    if wait > MAX_INLINE_WAIT or last:
                        raise RetryLater(max(wait, 1.0), f"{method} {where} answered {status}") from None
                    log.info("[connectors] %s %s answered %d; retrying in %.1fs", method, where, status, wait)
                    _sleep(wait)
                    continue
                if status in _RETRY_STATUSES and not last:
                    _sleep(2 ** (attempt - 1))
                    continue
                raise HttpError(status, f"{method} {where}: {detail}") from None
            except (urllib.error.URLError, socket.timeout, ConnectionError) as error:
                if last:
                    raise ConnectorError(f"{method} {where} failed: {type(error).__name__}") from None
                _sleep(2 ** (attempt - 1))
        raise ConnectorError(f"{method} {where} failed")  # not reached
