"""The console's pages: served only with CONSOLE_ENABLED, under a strict CSP, with no inline script or style.

``/privacy`` is served either way, and the public header's "Sign in" link shows only when the
console runs. No browser and no graph.
"""

import dataclasses
import re
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  pins the test settings before src loads

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api import console_web, web  # noqa: E402
from src.core.config import settings  # noqa: E402

STATIC = Path(__file__).resolve().parents[2] / "src" / "static"
CONSOLE_PAGES = sorted((STATIC / "console").glob("*.html"))
STRICT_PAGES = [*CONSOLE_PAGES, STATIC / "privacy.html"]


def _client(enabled: bool) -> tuple[TestClient, mock._patch]:
    patch = mock.patch.object(console_web, "settings", dataclasses.replace(settings, console_enabled=enabled))
    patch.start()
    app = FastAPI()
    web.install(app)
    console_web.install(app)
    return TestClient(app), patch


class OffByDefaultTest(unittest.TestCase):
    def setUp(self):
        self.http, patch = _client(False)
        self.addCleanup(patch.stop)

    def test_every_console_page_is_a_plain_404(self):
        self.assertTrue(console_web.PAGES)
        for route in console_web.PAGES:
            response = self.http.get(route.replace("{connection_id}", "jira:abc"))
            self.assertEqual(response.status_code, 404, route)
            self.assertEqual(response.json(), {"detail": "Not Found"}, route)

    def test_the_public_header_keeps_sign_in_hidden(self):
        for path in ("/", "/team/sales", "/explore"):
            page = self.http.get(path).text
            self.assertIn(console_web.CONSOLE_OFF, page, path)

    def test_privacy_is_served_with_the_strict_policy(self):
        response = self.http.get("/privacy")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-security-policy"], console_web.CSP)
        self.assertIn("Refactron", response.text)


class OnTest(unittest.TestCase):
    def setUp(self):
        self.http, patch = _client(True)
        self.addCleanup(patch.stop)

    def test_every_console_page_is_served_with_the_strict_policy(self):
        for route, name in console_web.PAGES.items():
            response = self.http.get(route.replace("{connection_id}", "jira:abc"))
            self.assertEqual(response.status_code, 200, route)
            self.assertEqual(response.text, (STATIC / "console" / name).read_text(), route)
            self.assertEqual(response.headers["content-security-policy"], console_web.CSP, route)
            self.assertEqual(response.headers["x-content-type-options"], "nosniff", route)

    def test_the_public_header_shows_sign_in(self):
        for path in ("/", "/team/sales", "/explore"):
            page = self.http.get(path).text
            self.assertIn(console_web.CONSOLE_ON, page, path)
            self.assertNotIn(console_web.CONSOLE_OFF, page, path)

    def test_the_policy_is_the_one_nginx_sets(self):
        self.assertEqual(
            console_web.CSP, "default-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self' https://slack.com"
        )


class StrictPageTest(unittest.TestCase):
    """What the CSP would block: inline scripts, inline styles, style attributes, off-site files."""

    def test_no_page_has_an_inline_script(self):
        for page in sorted(STATIC.glob("*.html")) + CONSOLE_PAGES:
            text = page.read_text()
            self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", text), [], page.name)

    def test_strict_pages_have_no_inline_style_and_load_only_this_sites_files(self):
        for page in STRICT_PAGES:
            text = page.read_text()
            self.assertNotIn("<style", text, page.name)
            self.assertNotRegex(text, r"\sstyle=", page.name)
            for resource in re.findall(r'<(?:script|link)[^>]+(?:src|href)="([^"]+)"', text):
                self.assertTrue(resource.startswith("/static/"), f"{page.name}: {resource}")

    def test_console_scripts_set_no_style_attribute_and_insert_text_only(self):
        for script in sorted((STATIC / "console").glob("*.js")) + [STATIC / "theme.js"]:
            text = script.read_text()
            self.assertIsNone(re.search(r"""["']style["']\s*[:,]""", text), script.name)
            self.assertNotIn("innerHTML", text, script.name)
            self.assertNotIn("insertAdjacentHTML", text, script.name)
            self.assertIsNone(re.search(r"https?://", text), script.name)

    def test_shared_scripts_a_console_page_loads_set_no_style_attribute(self):
        # graph.js, riskmap.js and answer.js draw the console's answers too; under the CSP a style
        # attribute is dropped, so they style through the CSSOM (element.style) only.
        loaded = {src for page in CONSOLE_PAGES for src in re.findall(r'<script src="/static/([^"?]+)', page.read_text())}
        self.assertIn("graph.js", loaded)
        for name in sorted(loaded):
            text = (STATIC / name).read_text()
            self.assertIsNone(re.search(r"""\bstyle:\s*[`"']|setAttribute\(\s*["']style""", text), name)

    def test_one_shared_header_on_every_page(self):
        # shell.js builds the nav, Sign in and the theme toggle from one definition (NAV); a page
        # carries only the brand and which page it is.
        shell = (STATIC / "shell.js").read_text()
        for key in (
            '["ask", "Ask", "/#ask"]',
            '["explore", "Explore", "/explore"]',
            '["connections", "Connections", "/console"]',
            '["ask", "Ask", "/console/ask"]',
        ):
            self.assertIn(key, shell)
        here = {"site": {"home", "team", "explore", "privacy", "signin"}, "console": {"connections", "start", "ask"}}
        for page in [*sorted(STATIC.glob("*.html")), *CONSOLE_PAGES]:
            text = page.read_text()
            match = re.search(r'<header class="top" data-shell="(site|console)" data-here="([a-z]+)">(.*?)</header>', text, re.S)
            self.assertIsNotNone(match, page.name)
            self.assertIn(match.group(2), here[match.group(1)], page.name)
            self.assertNotIn("<nav", match.group(3), page.name)
            self.assertIn('<div class="top-actions">', match.group(3), page.name)
            self.assertIn('id="theme-toggle"', match.group(3), page.name)
            self.assertIn('<meta name="brain-console" content="', text, page.name)
            self.assertIn('<script src="/static/shell.js', text, page.name)

    def test_every_page_stamps_the_theme_before_its_stylesheets(self):
        for page in sorted(STATIC.glob("*.html")) + CONSOLE_PAGES:
            text = page.read_text()
            self.assertIn('<script src="/static/theme.js', text, page.name)
            self.assertLess(text.index("/static/theme.js"), text.index("/static/style.css"), page.name)

    def test_console_css_sizes_are_type_scale_tokens(self):
        css = (STATIC / "console.css").read_text()
        for declaration in re.findall(r"font(?:-size)?:\s*([^;}]*)", css):
            self.assertFalse(re.search(r"\d(?:px|rem)\b", declaration), declaration)
