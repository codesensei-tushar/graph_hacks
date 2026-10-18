"""What a judge meets first: the README's first screen and its pictures, and a team page that never shows an empty box."""

import re
import unittest
from pathlib import Path

import tests  # noqa: F401  pins the test settings before src loads

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "static"


class ReadmeTest(unittest.TestCase):
    def setUp(self):
        self.readme = (ROOT / "README.md").read_text()

    def test_every_picture_exists_and_says_what_it_shows(self):
        pictures = [(alt, src) for alt, src in re.findall(r"!\[(.*?)\]\(([^)\s]+)\)", self.readme) if not src.startswith("http")]
        self.assertGreaterEqual(len(pictures), 5)
        for alt, src in pictures:
            self.assertTrue((ROOT / src).is_file(), src)
            self.assertGreater(len(alt.strip()), 30, f"{src} needs alt text that says what it shows")

    def test_the_first_screen_shows_the_product_and_the_judges_checklist(self):
        first = self.readme.split("## Try it in two minutes", 1)[0]
        self.assertIn("docs/images/01-who-should-look.png", first)
        self.assertIn("## For judges: verify it in two minutes", first)
        for link in (
            "https://brain.noobsurd.space",
            "/?q=what%20if%20arjun%20mehta%20leaves",
            "/?q=who%20knows%20the%20payments%20gateway",
            "#proof",
            "/team/payments",
            "claude mcp add --transport http company-brain https://brain.noobsurd.space/mcp",
            "scripts/replay_workspace.py",
            "scripts/check_live.py",
        ):
            self.assertIn(link, first)

    def test_readers_first_builders_after(self):
        order = [
            "## For judges",
            "## Try it in two minutes",
            "## What it answers",
            "## Graph data model",
            "## Cypher queries",
            "## The same tools over MCP",
            "## Run it locally",
            "## Docs",
            "## AI assistance",
            "## License",
        ]
        positions = [self.readme.index(heading) for heading in order]
        self.assertEqual(positions, sorted(positions))


class TeamPageStaticTest(unittest.TestCase):
    def test_the_team_page_has_its_switcher_note_links_and_risk(self):
        html = (STATIC / "team.html").read_text()
        script = (STATIC / "team.js").read_text()
        for element_id in ("team-switch", "team-missing", "team-links", "team-risk"):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("/static/riskmap.css", html)
        self.assertLess(html.index("/static/riskmap.js"), html.index("/static/team.js"))
        for needle in ("data.empty", "data.depends_on", "data.depended_on_by", 'fetch("/api/teams")', '"aria-current": "page"', "RiskMap.render"):
            self.assertIn(needle, script)
        # Empty sections are hidden, never filled with a sentence saying they are empty.
        self.assertNotIn("No decision about what this team runs", script)
        self.assertNotIn("Nothing said in a public channel", script)

    def test_the_risk_grid_scroller_holds_its_hidden_labels(self):
        # Without a positioned scroller, the cells' absolutely positioned screen-reader labels escape
        # the horizontal scroll and widen a page that has no clipping card around the grid.
        rule = re.search(r"\.riskmap-scroll \{([^}]*)\}", (STATIC / "riskmap.css").read_text()).group(1)
        self.assertIn("position: relative", rule)
