"""The graph explorer without a graph: what to follow from the focus, input limits, and checks on the page's own files."""

import re
import unittest
from pathlib import Path

import tests  # noqa: F401  pins the test settings before src loads

from src.domain.knowledge import explorer  # noqa: E402
from src.domain.knowledge.ask_api import QuestionTooLong  # noqa: E402
from src.domain.knowledge.dto import PathStep, ToolResult  # noqa: E402

STATIC = Path(__file__).resolve().parents[2] / "src" / "static"


def _step(source: tuple[str, str, str], rel: str, target: tuple[str, str, str]) -> PathStep:
    return PathStep(source[1], rel, target[1], from_id=source[0], to_id=target[0], from_label=source[2], to_label=target[2])


BILLING = ("billing-service", "Billing Service", "Service")
AUTH = ("auth-service", "Auth Service", "Service")
GATEWAY = ("api-gateway", "API Gateway", "Service")
ARJUN = ("arjun", "Arjun Mehta", "Person")
TICKET = ("TKT-102", "TKT-102", "Ticket")
DECISION = ("decision-billing-retries", "Retry renewal webhooks", "Decision")
MEMORY = ("memory-1", "billing now retries", "Memory")


class FollowsTest(unittest.TestCase):
    def test_one_follow_per_type_and_direction_at_the_focus_in_the_catalogue_order(self):
        result = ToolResult(tool="explore", answer="")
        result.extend(
            [
                _step(BILLING, "DEPENDS_ON", AUTH),
                _step(GATEWAY, "DEPENDS_ON", BILLING),
                _step(ARJUN, "OWNS", BILLING),
                _step(TICKET, "ABOUT", BILLING),
                _step(DECISION, "ABOUT", BILLING),
                _step(MEMORY, "MENTIONS", BILLING),
                # Not at the focus: a second-ring edge offers nothing to follow from here.
                _step(GATEWAY, "DEPENDS_ON", AUTH),
            ]
        )
        follows = [(follow.rel, follow.direction, follow.phrase, follow.count) for follow in explorer.follows("billing-service", result)]
        self.assertEqual(
            follows,
            [
                ("OWNS", "in", "owned by", 1),
                ("DEPENDS_ON", "out", "depends on", 1),
                ("DEPENDS_ON", "in", "depended on by", 1),
                ("ABOUT", "in", "decisions and tickets about it", 2),
                ("MENTIONS", "in", "mentioned in", 1),
            ],
        )

    def test_edges_neighbors_cannot_follow_offer_nothing(self):
        result = ToolResult(tool="explore", answer="")
        result.add(PathStep("Billing Service", "SAID", "x", from_id="billing-service", to_id="m", from_label="Service", to_label="Memory"))
        self.assertEqual(explorer.follows("billing-service", result), [])


class LimitsTest(unittest.TestCase):
    def test_a_node_name_is_trimmed_and_capped(self):
        self.assertEqual(explorer._clean("  the   sales\nteam "), "the sales team")
        with self.assertRaises(QuestionTooLong):
            explorer._clean("x" * (explorer.MAX_NODE_CHARS + 1))

    def test_depth_is_a_bounded_integer(self):
        self.assertEqual(explorer._clamp("9", 2), 2)
        self.assertEqual(explorer._clamp(0, 3), 1)
        self.assertEqual(explorer._clamp("deep", 3), 1)


class PageFilesTest(unittest.TestCase):
    def setUp(self):
        self.html = (STATIC / "explorer.html").read_text()
        self.script = (STATIC / "explorer.js").read_text()
        self.css = (STATIC / "explorer.css").read_text()

    def test_every_element_the_script_reads_is_on_the_page(self):
        ids = set(re.findall(r'\$\("([\w-]+)"\)', self.script))
        self.assertIn("explorer-graph", ids)
        missing = [element for element in sorted(ids) if f'id="{element}"' not in self.html]
        self.assertEqual(missing, [])

    def test_the_script_inserts_text_never_markup(self):
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(sink, self.script)

    def test_the_script_calls_only_the_explorer_routes(self):
        self.assertEqual(sorted(set(re.findall(r"`?(/api/[\w/]+)", self.script))), ["/api/explore", "/api/explore/neighbors", "/api/explore/start"])

    def test_the_styles_use_the_site_tokens_not_their_own_colours(self):
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", self.css), [])
        self.assertIn("var(--path)", self.css)
