"""The connector JSON client and the install registry, with no graph. Only the redirect test
opens a socket, to two servers on localhost."""

import io
import json
import logging
import threading
import unittest
import urllib.error
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.connectors import http, registry  # noqa: E402
from src.connectors.base import Connector, ConnectorError, CredentialsRejected, Install, RetryLater  # noqa: E402

SECRET = "s3cr3t-token-value"


class _Response:
    def __init__(self, body: object, headers: dict | None = None):
        self._raw = json.dumps(body).encode()
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _error(status: int, headers: dict | None = None, body: str = "{}") -> urllib.error.HTTPError:
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError("https://fake.example/x", status, "error", message, io.BytesIO(body.encode()))


class JsonClientTest(unittest.TestCase):
    def setUp(self):
        self.client = http.JsonClient("https://fake.example/rest", http.basic_auth("bot@fake.example", SECRET))
        self.sleeps: list[float] = []
        patch = mock.patch.object(http, "_sleep", self.sleeps.append)
        patch.start()
        self.addCleanup(patch.stop)

    def _serve(self, *outcomes):
        calls = []

        def urlopen(request, timeout):
            calls.append(request)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        patch = mock.patch.object(http, "_urlopen", urlopen)
        patch.start()
        self.addCleanup(patch.stop)
        return calls

    def test_get_sends_the_auth_header_and_parses_json(self):
        calls = self._serve(_Response({"ok": 1}))
        self.assertEqual(self.client.get("/api/3/myself", {"expand": "groups"}), {"ok": 1})
        self.assertEqual(calls[0].full_url, "https://fake.example/rest/api/3/myself?expand=groups")
        self.assertTrue(calls[0].get_header("Authorization").startswith("Basic "))

    def test_short_retry_after_waits_in_place(self):
        self._serve(_error(429, {"Retry-After": "2"}), _Response({"ok": 1}))
        self.assertEqual(self.client.get("x"), {"ok": 1})
        self.assertEqual(self.sleeps, [2.0])

    def test_long_retry_after_hands_back_to_the_scheduler(self):
        self._serve(_error(429, {"Retry-After": "120"}))
        with self.assertRaises(RetryLater) as raised:
            self.client.get("x")
        self.assertEqual(raised.exception.seconds, 120.0)
        self.assertEqual(self.sleeps, [])

    def test_429_without_a_wait_uses_the_default(self):
        self._serve(_error(429))
        with self.assertRaises(RetryLater) as raised:
            self.client.get("x")
        self.assertEqual(raised.exception.seconds, http.DEFAULT_RETRY_AFTER)

    def test_server_errors_back_off_then_give_up(self):
        self._serve(_error(502), _Response({"ok": 1}))
        self.assertEqual(self.client.get("x"), {"ok": 1})
        self.assertEqual(self.sleeps, [1])
        self._serve(_error(500), _error(500), _error(500))
        with self.assertRaises(http.HttpError) as raised:
            self.client.get("x")
        self.assertEqual(raised.exception.status, 500)

    def test_network_errors_back_off_then_give_up(self):
        self._serve(urllib.error.URLError("down"), urllib.error.URLError("down"), urllib.error.URLError("down"))
        with self.assertRaises(ConnectorError):
            self.client.get("x")
        self.assertEqual(self.sleeps, [1, 2])

    def test_401_is_rejected_credentials_and_never_quotes_them(self):
        self._serve(_error(401, body=f"bad token {SECRET}"))
        with self.assertRaises(CredentialsRejected) as raised:
            self.client.get("x")
        self.assertNotIn(SECRET, str(raised.exception))

    def test_other_client_errors_carry_the_status(self):
        self._serve(_error(404))
        with self.assertRaises(http.HttpError) as raised:
            self.client.get("x")
        self.assertEqual(raised.exception.status, 404)

    def test_credentials_never_go_to_another_host(self):
        calls = self._serve(_Response({}))
        with self.assertRaises(ConnectorError):
            self.client.get("https://evil.example/rest/next")
        self.assertEqual(calls, [])
        self.client.get("https://fake.example/rest/next?page=2")
        self.assertEqual(calls[0].full_url, "https://fake.example/rest/next?page=2")
        self.assertNotIn(SECRET, repr(self.client))

    def test_retry_after_reads_seconds_and_dates(self):
        self.assertEqual(http.retry_after_seconds("7"), 7.0)
        self.assertIsNone(http.retry_after_seconds(None))
        self.assertEqual(http.retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT"), 0.0)

    def test_waits_are_clamped_to_an_hour(self):
        self.assertEqual(http.retry_after_seconds("999999999"), 3600.0)
        self.assertEqual(http.retry_after_seconds("Fri, 01 Jan 2100 00:00:00 GMT"), 3600.0)
        self.assertEqual(RetryLater(999_999_999).seconds, 3600.0)
        self.assertEqual(RetryLater(float("nan")).seconds, 0.0)
        self._serve(_error(429, {"Retry-After": "999999999"}))
        with self.assertRaises(RetryLater) as raised:
            self.client.get("x")
        self.assertEqual(raised.exception.seconds, 3600.0)


class _Server:
    """A local HTTP server; ``reply(handler)`` answers each GET, and every request's headers are kept."""

    def __init__(self, reply):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(dict(self.headers))
                reply(self)

            def log_message(self, *_):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class RedirectTest(unittest.TestCase):
    def test_a_redirect_is_never_followed_so_credentials_stay_home(self):
        def ok(handler):
            handler.send_response(200)
            handler.end_headers()
            handler.wfile.write(b"{}")

        elsewhere = _Server(ok)
        self.addCleanup(elsewhere.close)
        for status in (301, 302, 307, 308):

            def redirect(handler, status=status):
                handler.send_response(status)
                handler.send_header("Location", f"{elsewhere.url}/steal")
                handler.end_headers()

            source = _Server(redirect)
            self.addCleanup(source.close)
            client = http.JsonClient(source.url, http.bearer(SECRET))
            with self.assertRaises(http.HttpError) as raised:
                client.get("/rest/api/3/search")
            self.assertEqual(raised.exception.status, status)
            self.assertEqual(len(source.seen), 1)
        self.assertEqual(elsewhere.seen, [])


class _Needs(Connector):
    source = "needs"
    required = ("base_url", "api_token")


class RegistryTest(unittest.TestCase):
    def setUp(self):
        registry.register(_Needs())
        self.addCleanup(registry._connectors.pop, "needs", None)
        self.addCleanup(registry.unconfigure, "needs")

    def test_an_install_needs_every_required_field(self):
        with self.assertLogs("src.connectors.registry", logging.WARNING) as logs:
            self.assertEqual(registry.load({"CONNECTOR_NEEDS_API_TOKEN": SECRET}), [])
        self.assertIn("CONNECTOR_NEEDS_BASE_URL", logs.output[0])
        self.assertNotIn(SECRET, " ".join(logs.output))
        self.assertIsNone(registry.install_for("needs"))

    def test_env_fields_become_the_install_config(self):
        (install,) = registry.load({"CONNECTOR_NEEDS_API_TOKEN": SECRET, "CONNECTOR_NEEDS_BASE_URL": "https://x", "CONNECTOR_NEEDS_INSTALL_ID": "acme", "OTHER": "1"})
        self.assertEqual((install.source, install.id, install.key), ("needs", "acme", "needs:acme"))
        self.assertEqual(install.get("api_token"), SECRET)
        self.assertNotIn(SECRET, repr(install))
        self.assertIs(registry.install_for("needs"), install)
        self.assertEqual(install.item_id(12), "needs:acme:12")
        self.assertEqual(install.user_id("a"), "needs:acme:user:a")
        self.assertEqual(install.container_id("PAY"), "needs:acme:container:PAY")

    def test_bad_names_are_refused(self):
        class Bad(Connector):
            source = "Bad Name"

        with self.assertRaises(ValueError):
            registry.register(Bad())
        with self.assertRaises(ValueError):
            registry.configure(Install(source="needs", id="no spaces"))

    def test_list_fields(self):
        self.assertEqual(Install("needs", config={"projects": " PAY, OPS ,,"}).get_list("projects"), ["PAY", "OPS"])


if __name__ == "__main__":
    unittest.main()
