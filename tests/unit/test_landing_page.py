"""The public page's static contract, checked without a browser (docs/DESIGN.md, sections 2 and 4).

Every section is on the page with its heading; nothing the API counts is typed into the HTML;
the page asks only for its own routes and vendored files; and ``/team/{id}`` still renders.
"""

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

import tests  # noqa: F401  pins the test settings before src loads

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api import web  # noqa: E402
from src.domain.knowledge.algorithms import depth_in  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "static"
PAGE_SCRIPTS = ("shell.js", "graph.js", "hero.js", "app.js")
ALLOWED_ROUTES = {"/api/ask", "/api/ask/tools", "/api/ask/proof", "/api/teams", "/api/team/", "/api/graph/stats", "/api/graph/sample"}
VOID = {"meta", "link", "input", "br", "img", "hr", "source", "path", "circle", "rect", "polygon", "line"}


class _Page(HTMLParser):
    """Headings in order, the text under every element with an id, and every resource the page loads."""

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, str | None]] = []
        self.headings: list[tuple[str, str]] = []
        self.text_by_id: dict[str, str] = {}
        self.stat_cells: list[str] = []
        self.resources: list[str] = []
        self.tags: list[str] = []
        self._heading: list | None = None
        self._stat: list | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append(tag)
        if tag == "script" and attrs.get("src"):
            self.resources.append(attrs["src"])
        if tag == "link" and attrs.get("href"):
            self.resources.append(attrs["href"])
        if tag in {"img", "iframe", "source", "video", "audio"}:
            self.resources.append(attrs.get("src", ""))
        if tag in VOID:
            return
        element_id = attrs.get("id")
        if element_id:
            self.text_by_id[element_id] = ""
        self.stack.append((tag, element_id))
        if tag in {"h1", "h2"}:
            self._heading = [tag, ""]
        if tag == "dd" and "data-stat" in attrs:
            self._stat = [""]

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        while self.stack:
            open_tag, _ = self.stack.pop()
            if open_tag == tag:
                break
        if self._heading and tag == self._heading[0]:
            self.headings.append((tag, " ".join(self._heading[1].split())))
            self._heading = None
        if self._stat is not None and tag == "dd":
            self.stat_cells.append(self._stat[0])
            self._stat = None

    def handle_data(self, data):
        for _, element_id in self.stack:
            if element_id:
                self.text_by_id[element_id] += data
        if self._heading:
            self._heading[1] += data
        if self._stat is not None:
            self._stat[0] += data


def _parse(name: str) -> _Page:
    page = _Page()
    page.feed((STATIC / name).read_text())
    return page


class SectionsTest(unittest.TestCase):
    def test_every_section_of_the_brief_is_on_the_page_with_its_heading(self):
        page = _parse("index.html")
        self.assertEqual(
            page.headings,
            [
                ("h1", "Ask how your company works. Every answer shows the path it walked."),
                ("h2", "Ask the graph behind this project."),
                ("h2", "Take FalkorDB out, and the answers go with it."),
                ("h2", "Not only engineering."),
                ("h2", "How an answer is made."),
                ("h2", "The same tools, over MCP."),
            ],
        )
        for landmark in ("header", "main", "footer"):
            self.assertEqual(page.tags.count(landmark), 1, landmark)
        for section in ("ask", "live-answer", "proof", "teams", "how", "builders"):
            self.assertIn(section, page.text_by_id)

    def test_the_footer_carries_the_readme_ai_disclosure_word_for_word(self):
        disclosure = next(line for line in (ROOT / "README.md").read_text().splitlines() if line.startswith("AI assistance:"))
        self.assertIn(disclosure, (STATIC / "index.html").read_text())

    def test_the_answer_region_is_announced(self):
        self.assertRegex((STATIC / "index.html").read_text(), r'class="answer-body" aria-live="polite"')


class NoTypedNumbersTest(unittest.TestCase):
    def test_counts_that_come_from_the_api_are_empty_in_the_html(self):
        page = _parse("index.html")
        self.assertEqual(len(page.stat_cells), 4)
        self.assertEqual([cell.strip() for cell in page.stat_cells], ["", "", "", ""])
        for element_id in ("live-count", "proof-line", "proof-refusal", "proof-body", "tool-count", "tool-list", "team-cards", "chips"):
            self.assertNotRegex(page.text_by_id[element_id], r"\d", element_id)


class RequestsTest(unittest.TestCase):
    def test_the_page_loads_only_its_own_files(self):
        for name in ("index.html", "team.html"):
            for resource in _parse(name).resources:
                self.assertTrue(resource.startswith("/static/") or resource.startswith("data:"), f"{name}: {resource}")

    def test_scripts_call_only_the_public_routes_and_nothing_off_site(self):
        for script in STATIC.glob("*.js"):
            text = script.read_text()
            for route in re.findall(r"/api/[a-z/]*", text):
                self.assertIn(route, ALLOWED_ROUTES, f"{script.name}: {route}")
            self.assertEqual(set(re.findall(r"https?://[^\s\"'`)]+", text)) - {"http://www.w3.org/2000/svg"}, set(), script.name)

    def test_the_stylesheet_loads_only_the_vendored_fonts(self):
        urls = re.findall(r"url\(\s*[\"']?([^\"')]+)", (STATIC / "style.css").read_text())
        self.assertTrue(urls)
        for url in urls:
            self.assertTrue(url.startswith("/static/fonts/") or url.startswith("#"), url)


class WeightTest(unittest.TestCase):
    def test_two_vendored_fonts_with_their_licence_and_the_page_under_its_budget(self):
        fonts = sorted(path.name for path in (STATIC / "fonts").glob("*.woff2"))
        self.assertEqual(fonts, ["Geist-Variable.woff2", "GeistMono-Variable.woff2"])
        self.assertTrue((STATIC / "fonts" / "OFL.txt").exists())
        own_js = sum((STATIC / name).stat().st_size for name in PAGE_SCRIPTS)
        self.assertLess(own_js, 80 * 1024)
        page = own_js + sum((STATIC / name).stat().st_size for name in ("index.html", "style.css")) + sum(
            (STATIC / "fonts" / name).stat().st_size for name in fonts
        )
        self.assertLess(page, 300 * 1024)


class TeamPageTest(unittest.TestCase):
    def test_a_team_page_still_renders_with_the_shared_shell(self):
        app = FastAPI()
        web.install(app)
        http = TestClient(app)
        page = http.get("/team/sales")
        self.assertEqual(page.status_code, 200)
        for asset in ("/static/style.css", "/static/shell.js", "/static/graph.js", "/static/team.js"):
            self.assertIn(asset, page.text)
            self.assertEqual(http.get(asset).status_code, 200, asset)
        self.assertIn('id="theme-toggle"', page.text)


def _js_regex(text: str, name: str) -> re.Pattern:
    """A regex literal from a page script, compiled the way the page runs it (only ``/i`` is used)."""
    source = re.search(rf"const {name} = /(.+)/i;", text).group(1)
    return re.compile(source, re.IGNORECASE)


class DepthButtonsTest(unittest.TestCase):
    """The "1 hop / 2 hops / all" buttons on a blast_radius answer ask what the server reads as that depth."""

    def setUp(self):
        self.app_js = (STATIC / "app.js").read_text()
        self.hop_phrase = _js_regex(self.app_js, "HOP_PHRASE")
        self.names_depth = _js_regex(self.app_js, "NAMES_DEPTH")
        depths = re.search(r"const DEPTHS = \[(.+)\];", self.app_js).group(1)
        self.depths = [(label, phrase, None if hops == "null" else int(hops)) for label, phrase, hops in re.findall(r'\["([^"]+)", "([^"]*)", (\d+|null)\]', depths)]

    def base(self, question: str) -> str:
        return self.hop_phrase.sub("", question, count=1)

    def test_three_buttons_on_found_blast_radius_answers_only(self):
        self.assertEqual([label for label, _, _ in self.depths], ["1 hop", "2 hops", "all"])
        self.assertEqual(len(re.findall(r"addDepthButtons\(", self.app_js)), 2, "defined once, called once")
        self.assertIn('if (data.tool === "blast_radius" && data.found) addDepthButtons(question, data.depth);', self.app_js)

    def test_the_buttons_ask_the_pinned_phrasings(self):
        self.assertEqual([phrase for _, phrase, _ in self.depths], [", one hop", ", two hops out", ""])

    def test_a_trailing_hop_phrase_is_dropped_to_find_the_base(self):
        for question in (
            "what breaks if auth goes down, two hops out",
            "what breaks if auth goes down, one hop",
            "what breaks if auth goes down, 3 hops",
            "What breaks if auth goes down, Two Hops Out?",
            "what breaks if auth goes down?",
            "what breaks if auth goes down",
        ):
            self.assertEqual(self.base(question).lower(), "what breaks if auth goes down", question)

    def test_each_button_asks_the_depth_the_server_reads(self):
        base = self.base("what breaks if auth goes down, one hop")
        self.assertIsNone(depth_in(base))
        for label, phrase, hops in self.depths:
            self.assertEqual(depth_in(f"{base}{phrase}"), hops, label)

    def test_the_pressed_button_follows_the_servers_reading_of_the_question(self):
        for question in (
            "what breaks if auth goes down",
            "what breaks if auth goes down, one hop",
            "what breaks if auth goes down, two hops out",
            "what breaks if auth goes down, 4 hops",
            "what breaks at depth 3 if auth goes down",
            "what directly breaks if auth goes down",
            "who are the direct dependents of auth",
            "what is immediately affected if auth goes down",
            "what breaks on the first hop from auth",
            "what breaks if the hop service goes down",
        ):
            self.assertEqual(bool(self.names_depth.search(question)), depth_in(question) is not None, question)


class CopyLinkTest(unittest.TestCase):
    """ "Copy link" on an answer copies the address a shared link opens on."""

    def setUp(self):
        self.app_js = (STATIC / "app.js").read_text()

    def test_the_copied_link_is_the_one_the_page_reads_on_load(self):
        self.assertIn("new URL(`/?q=${encodeURIComponent(question)}`, location.origin).href", self.app_js)
        self.assertIn('new URLSearchParams(location.search).get("q")', self.app_js)
        self.assertIn('history.replaceState(null, "", `?q=${encodeURIComponent(q)}`)', self.app_js)

    def test_every_answer_with_a_path_gets_the_button_beside_replay(self):
        self.assertEqual(len(re.findall(r"addCopyLink\(", self.app_js)), 2, "defined once, called once")
        self.assertRegex(self.app_js, r"addDepthButtons\(question, data\.depth\);\n    addCopyLink\(question\);")
        self.assertIn('head.insertBefore(copy, $(".replay", head));', self.app_js)
        for label in ('"Copy link"', '"Copied"', '"Not copied"'):
            self.assertIn(label, self.app_js)
