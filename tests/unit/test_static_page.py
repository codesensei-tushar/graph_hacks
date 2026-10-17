"""Checks on the page scripts that need no browser.

``append`` and ``replaceChildren`` return ``undefined`` in the DOM, so chaining a property
off their result throws at runtime. The live page once failed every answer with a path
that way ("Cannot read properties of undefined (reading 'lastChild')").
"""

import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "src" / "static"
CHAINED = re.compile(r"\.(?:append|prepend|replaceChildren)\((?:[^()]|\([^()]*\))*\)\s*\.\s*\w")


class PageScriptTest(unittest.TestCase):
    def test_no_property_is_read_off_a_dom_insert_that_returns_nothing(self):
        scripts = sorted(STATIC.glob("*.js"))
        self.assertIn("app.js", [script.name for script in scripts])
        offenders = [f"{script.name}: {line.strip()}" for script in scripts for line in script.read_text().splitlines() if CHAINED.search(line)]
        self.assertEqual(offenders, [])

    def test_the_pattern_catches_the_bug_it_guards_against(self):
        self.assertRegex('svg.append(svgEl("title")).lastChild.textContent = "x";', CHAINED)
        self.assertRegex('body.replaceChildren(el("tr", {}, "")).firstChild.append(cell);', CHAINED)
        self.assertNotRegex("group.append(tip);", CHAINED)
