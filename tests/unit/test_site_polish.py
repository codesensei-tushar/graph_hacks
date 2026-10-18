"""The public pages' link previews, the phone menu, and the 404 a browser gets for a mistyped address."""

import re
import struct
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import console_web, web

STATIC = Path(__file__).resolve().parents[2] / "src" / "static"
PAGES = ("index.html", "team.html", "explorer.html")


def _meta(html: str, key: str) -> str | None:
    match = re.search(rf'<meta (?:property|name)="{re.escape(key)}" content="([^"]*)">', html)
    return match.group(1) if match else None


class LinkPreviewTest(unittest.TestCase):
    def test_every_public_page_has_a_preview_card(self):
        for name in PAGES:
            html = (STATIC / name).read_text()
            with self.subTest(page=name):
                for key in ("og:title", "og:description", "og:site_name", "og:image", "og:image:alt"):
                    self.assertTrue(_meta(html, key), key)
                self.assertEqual(_meta(html, "og:type"), "website")
                self.assertEqual(_meta(html, "twitter:card"), "summary_large_image")
                # Slack, LinkedIn and X only fetch an absolute image URL.
                self.assertEqual(_meta(html, "og:image"), "https://brain.noobsurd.space/static/og.png")

    def test_the_preview_image_is_the_size_cards_expect(self):
        data = (STATIC / "og.png").read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", data[16:24])
        self.assertEqual((width, height), (1200, 630))


class PhoneMenuTest(unittest.TestCase):
    def test_every_page_folds_its_sections_behind_a_menu_button(self):
        for name in (*PAGES, "privacy.html", "console/index.html", "console/signin.html"):
            for console_on in (False, True):
                html = console_web.render((STATIC / name).read_text(), console_on=console_on)
                with self.subTest(page=name, console_on=console_on):
                    self.assertEqual(html.count('id="nav-toggle"'), 1)
                    self.assertIn('<button class="icon-btn nav-toggle" id="nav-toggle" type="button" aria-label="Menu" aria-expanded="false">', html)
                    self.assertIn("<nav aria-label=", html)

    def test_the_shared_script_opens_and_closes_it(self):
        script = (STATIC / "shell.js").read_text()
        self.assertIn('getElementById("nav-toggle")', script)
        self.assertIn("aria-expanded", script)
        self.assertIn("Escape", script)


class NotFoundPageTest(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        web.install(app)
        self.http = TestClient(app)

    def test_a_browser_gets_a_page_that_leads_back(self):
        response = self.http.get("/no-such-page", headers={"Accept": "text/html,application/xhtml+xml"})
        self.assertEqual(response.status_code, 404)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("This page is not here.", response.text)
        self.assertIn('href="/"', response.text)
        self.assertIn('<nav aria-label="Sections">', response.text)
        self.assertIn('id="nav-toggle"', response.text)

    def test_api_clients_and_api_paths_keep_the_json_404(self):
        self.assertEqual(self.http.get("/no-such-page").json(), {"detail": "Not Found"})
        html = {"Accept": "text/html"}
        for path in ("/api/no-such-route", "/static/nope.js", "/mcp/nope"):
            with self.subTest(path=path):
                response = self.http.get(path, headers=html)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("This page is not here.", response.text)


if __name__ == "__main__":
    unittest.main()
