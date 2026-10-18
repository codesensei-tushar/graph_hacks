"""The token endpoint calls in src/connectors/oauth.py, without a network or a database."""

import io
import json
import unittest
import urllib.error
from unittest import mock

import tests  # noqa: F401

from src.connectors import http, oauth  # noqa: E402
from src.connectors.base import RetryLater  # noqa: E402
from src.connectors.hubspot import oauth as hubspot_oauth  # noqa: E402
from src.connectors.jira import oauth as jira_oauth  # noqa: E402


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(status: int, payload: dict, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://vendor.test/token", status, "x", headers or {}, io.BytesIO(json.dumps(payload).encode()))


class TokenEndpointTest(unittest.TestCase):
    def setUp(self):
        self.sent = []

    def _answer(self, payload=None, error=None):
        def urlopen(request, timeout):
            self.sent.append(request)
            if error is not None:
                raise error
            return _Response(json.dumps(payload).encode())

        return mock.patch.object(http, "_urlopen", urlopen)

    def test_atlassian_takes_json_and_hubspot_a_form(self):
        env = {"JIRA_OAUTH_CLIENT_ID": "jid", "JIRA_OAUTH_CLIENT_SECRET": "js", "HUBSPOT_OAUTH_CLIENT_ID": "hid", "HUBSPOT_OAUTH_CLIENT_SECRET": "hs"}
        with mock.patch.dict("os.environ", env), self._answer({"access_token": "a", "refresh_token": "r", "expires_in": 3600}):
            oauth.exchange_code(jira_oauth.SPEC, "code-1", "https://brain.test/cb")
            oauth.exchange_code(hubspot_oauth.SPEC, "code-2", "https://brain.test/cb")
        jira, hubspot = self.sent
        self.assertEqual((jira.full_url, jira.get_header("Content-type")), ("https://auth.atlassian.com/oauth/token", "application/json"))
        self.assertEqual(json.loads(jira.data)["client_secret"], "js")
        self.assertEqual((hubspot.full_url, hubspot.get_header("Content-type")), ("https://api.hubapi.com/oauth/2026-09/token", "application/x-www-form-urlencoded"))
        self.assertIn(b"grant_type=authorization_code", hubspot.data)

    def test_a_refusal_carries_the_error_code_only(self):
        error = _http_error(400, {"error": "invalid_grant", "error_description": "token r-secret-123 is unknown"})
        with self._answer(error=error), self.assertRaises(oauth.OAuthError) as raised:
            oauth.refresh(jira_oauth.SPEC, "r-secret-123")
        self.assertEqual((raised.exception.code, raised.exception.status), ("invalid_grant", 400))
        self.assertNotIn("r-secret-123", str(raised.exception))
        odd = _http_error(400, {"error": "<b>not a code</b>"})
        with self._answer(error=odd), self.assertRaises(oauth.OAuthError) as raised:
            oauth.refresh(jira_oauth.SPEC, "r")
        self.assertEqual(raised.exception.code, "unknown")

    def test_a_rate_limit_or_outage_waits(self):
        with self._answer(error=_http_error(429, {}, {"Retry-After": "12"})), self.assertRaises(RetryLater) as raised:
            oauth.refresh(jira_oauth.SPEC, "r")
        self.assertEqual(raised.exception.seconds, 12)

    def test_no_refresh_token_is_refused_at_connect(self):
        with self._answer({"access_token": "a", "expires_in": 3600}), self.assertRaises(oauth.OAuthError) as raised:
            oauth.exchange_code(jira_oauth.SPEC, "c", "https://brain.test/cb")
        self.assertEqual(raised.exception.code, "no_refresh_token")

    def test_a_refresh_that_does_not_rotate_keeps_the_refresh_token(self):
        with self._answer({"access_token": "a2", "expires_in": 1800}):
            tokens = oauth.refresh(hubspot_oauth.SPEC, "r1")
        self.assertEqual((tokens.access_token, tokens.refresh_token), ("a2", "r1"))
        self.assertNotIn("a2", repr(tokens))

    def test_secret_fields_cover_every_credential(self):
        from src.connectors.hubspot.connector import HubSpotConnector
        from src.connectors.jira import JiraConnector

        self.assertEqual(JiraConnector.secret_fields, ("api_token", "access_token", "refresh_token", "expires_at"))
        self.assertEqual(HubSpotConnector.secret_fields, ("token", "access_token", "refresh_token", "expires_at"))
        self.assertEqual(set(oauth.Tokens("a", "r", 1.0).as_secret()), set(oauth.OAUTH_SECRET_FIELDS))

    def test_site_urls(self):
        for good in ("https://acme.atlassian.net", "https://a-1.atlassian.net"):
            self.assertTrue(jira_oauth.SITE_URL.fullmatch(good))
        for bad in ("http://acme.atlassian.net", "https://acme.atlassian.net.evil.com", "https://acme.atlassian.net/", "https://-x.atlassian.net", "https://a.b.atlassian.net", "https://jira.example.com"):
            self.assertFalse(jira_oauth.SITE_URL.fullmatch(bad), bad)


if __name__ == "__main__":
    unittest.main()
