"""Checks on the page script that need no browser.

``append`` and ``replaceChildren`` return ``undefined`` in the DOM, so chaining a property
off their result throws at runtime. The live page once failed every answer with a path
that way ("Cannot read properties of undefined (reading 'lastChild')").
"""

import re
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "src" / "static" / "app.js"
CHAINED = re.compile(r"\.(?:append|prepend|replaceChildren)\((?:[^()]|\([^()]*\))*\)\s*\.\s*\w")


class PageScriptTest(unittest.TestCase):
    def test_no_property_is_read_off_a_dom_insert_that_returns_nothing(self):
        source = SCRIPT.read_text()
        offenders = [line.strip() for line in source.splitlines() if CHAINED.search(line)]
        self.assertEqual(offenders, [])

    def test_the_pattern_catches_the_bug_it_guards_against(self):
        self.assertRegex('svg.append(svgEl("title")).lastChild.textContent = "x";', CHAINED)
        self.assertRegex('body.replaceChildren(el("tr", {}, "")).firstChild.append(cell);', CHAINED)
        self.assertNotRegex("group.append(tip);", CHAINED)
