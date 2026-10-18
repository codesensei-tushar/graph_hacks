"""Exploring from an answer: every question the page offers routes to the tool it is meant for.

The page offers a node's questions from graph.js's QUESTIONS (the hero, the answer drawing,
"Try instead") and a tool's follow-ups from app.js's NEXT ("Ask next"). This checks each
template against the keyword router, filled with names of the right label from the Quillfern
seed and its Sales team, and holds every NEXT step to a QUESTIONS template, so the integration
test that asks QUESTIONS on a fresh seed covers every chip. No browser and no graph.
"""

import re
import unittest
from pathlib import Path

import tests  # noqa: F401  pins the test settings before src loads

from src.domain.knowledge import algorithms, ask_service, catalogue  # noqa: E402
from tests.page_questions import NEXT, QUESTIONS, fill, questions_for  # noqa: E402

STATIC = Path(__file__).resolve().parents[2] / "src" / "static"

# Each template, the tool the router must pick for it, and the direction for blast_radius.
TOOL = {
    "who knows {name}": ("who_knows", None),
    "what breaks if {name} goes down": ("blast_radius", None),
    "what does {name} depend on": ("blast_radius", algorithms.DEPENDENCIES),
    "what if {name} leaves": ("if_person_leaves", None),
    "who is on the {name} team": ("onboarding_brief", None),
    "why did we {lower}": ("decision_trace", None),
    "what is {name} about": ("who_knows", None),
}
# Names each key covers on the Quillfern seed and its Sales team: every kind a Service can have there.
NAMES = {
    "Service": ["Billing Service", "Auth Service", "Memory Layer", "Graph Store", "API Gateway", "Renewals", "HubSpot", "Pricing playbook"],
    "account": ["Enterprise accounts"],
    "Person": ["Arjun Mehta", "Sara Lindqvist", "Priya Shah", "Noor Haddad"],
    "Team": ["Payments", "Platform", "Data", "Sales"],
    "Decision": [
        "Move the memory layer off Postgres",
        "Keep notebook links in Postgres join tables",
        "Retry renewal webhooks inside the billing service",
        "Discount monthly plans at renewal",
    ],
    "Ticket": ["TKT-101", "TKT-102"],
    "Document": ["Billing runbook", "RFC: memory layer on a graph store"],
    "": ["Billing Service", "API Gateway"],
}
# A NEXT step names one node by its role; the role's label says which QUESTIONS it must come from.
SLOT_LABEL = {"service": "Service", "owner": "Person", "proposer": "Person", "person": "Person"}
# A NEXT step that names no node is a question on its own.
STANDALONE = {"which services are load-bearing": "load_bearing"}


class QuestionsRouteTest(unittest.TestCase):
    def test_every_key_has_names_and_every_template_has_a_tool(self):
        self.assertEqual(set(QUESTIONS), set(NAMES))
        for key, templates in QUESTIONS.items():
            for template in templates:
                self.assertIn(template, TOOL, key)

    def test_every_question_routes_to_its_tool(self):
        for key, templates in QUESTIONS.items():
            for template in templates:
                tool, direction = TOOL[template]
                for name in NAMES[key]:
                    question = fill(template, name)
                    with self.subTest(question=question):
                        picked, args = ask_service.route(question)
                        self.assertEqual(picked, tool)
                        self.assertEqual(args.get("direction"), direction)

    def test_a_service_kind_without_its_own_entry_asks_what_a_service_asks(self):
        self.assertEqual(questions_for("Renewals", "Service", "process"), questions_for("Renewals", "Service", "service"))
        self.assertEqual(questions_for("Enterprise accounts", "Service", "account"), ["who knows Enterprise accounts"])
        self.assertEqual(questions_for("notebook#52", "PullRequest"), [])
        self.assertEqual(questions_for("payments gateway"), ["who knows payments gateway"])

    def test_a_decision_reads_as_a_why_question(self):
        self.assertEqual(questions_for("Move the memory layer off Postgres", "Decision"), ["why did we move the memory layer off Postgres"])
        self.assertEqual(questions_for("RFC first", "Decision"), ["why did we RFC first"])


class NextStepsTest(unittest.TestCase):
    def test_every_tool_with_steps_is_a_read_tool(self):
        for tool in NEXT:
            self.assertIn(tool, catalogue.tool_names())
            self.assertFalse(catalogue.get_tool(tool).needs_approval, tool)

    def test_every_step_is_a_node_question_or_a_question_on_its_own(self):
        for tool, steps in NEXT.items():
            for step in steps:
                with self.subTest(tool=tool, step=step):
                    slots = re.findall(r"\{(\w+)\}", step)
                    if not slots:
                        self.assertEqual(ask_service.route(step)[0], STANDALONE[step])
                        continue
                    self.assertEqual(len(slots), 1)
                    self.assertIn(step.replace(f"{{{slots[0]}}}", "{name}"), QUESTIONS[SLOT_LABEL[slots[0]]])


class WiringTest(unittest.TestCase):
    def setUp(self):
        self.graph_js = (STATIC / "graph.js").read_text()
        self.hero_js = (STATIC / "hero.js").read_text()
        self.app_js = (STATIC / "app.js").read_text()
        self.team_js = (STATIC / "team.js").read_text()

    def test_the_hero_asks_what_the_answers_ask(self):
        self.assertIn("questionsFor(node)[0]", self.hero_js)
        self.assertNotIn("who knows ${node.name}", self.hero_js)
        self.assertNotIn("ASKABLE", self.hero_js)
        self.assertIn("questionsFor,", self.graph_js.split("window.BrainGraph = {")[1])

    def test_answer_drawings_ask_and_team_drawings_stay_passive(self):
        self.assertIn("onAsk: askFromPage", self.app_js)
        self.assertNotIn("onAsk", self.team_js)
        self.assertIn("Click to ask: ${question}", self.graph_js)

    def test_ask_next_and_try_instead_are_labelled_lists_of_buttons(self):
        self.assertIn('chipRow(data.found ? "Ask next" : "Try instead", next)', self.app_js)
        self.assertIn('el("ul", { class: "next-list", "aria-label": label })', self.app_js)
        self.assertIn("const MAX_NEXT = 3;", self.app_js)

    def test_the_row_comes_after_the_edge_lines_and_before_the_footer(self):
        render = self.app_js[self.app_js.index("function render(data, question)"):]
        self.assertLess(render.index("BrainGraph.renderPath("), render.index("chipRow("))
        self.assertLess(render.index("chipRow("), render.index('el("p", { class: "footer-line" })'))


class WalkTest(unittest.TestCase):
    """Every ask is a history entry; Back and Forward show the step's answer from memory."""

    def setUp(self):
        self.app_js = (STATIC / "app.js").read_text()
        self.popstate = self.app_js[self.app_js.index('window.addEventListener("popstate"'):self.app_js.index("function walkRow()")]

    def test_a_deep_link_starts_the_walk_without_a_second_entry(self):
        self.assertIn('if (initial) { input.value = initial; ask(initial, true, "start"); }', self.app_js)
        start = self.app_js[self.app_js.index('} else if (how === "start") {'):self.app_js.index("} else if (walk.at >= 0")]
        self.assertIn("history.replaceState(", start)
        self.assertNotIn("pushState", start)

    def test_back_and_forward_render_the_steps_answer_without_asking_or_pinning(self):
        self.assertIn("render(entry.data, q);", self.popstate)
        self.assertNotIn("getJson(", self.popstate)
        self.assertNotIn("pinAnswer(", self.popstate)
        # Only an entry that outlived a reload asks again, once, and starts a new walk.
        self.assertIn('if (!entry || entry.q !== q || !entry.data) { ask(q, false, "back", state.step); return; }', self.popstate)

    def test_the_walk_lives_in_memory_only(self):
        for storage in ("localStorage", "sessionStorage"):
            self.assertNotIn(storage, self.app_js[self.app_js.index("// --- The walk"):self.app_js.index("async function ask(")])

    def test_your_walk_comes_after_ask_next_and_before_the_footer(self):
        render = self.app_js[self.app_js.index("function render(data, question)"):]
        self.assertLess(render.index("chipRow("), render.index("walkRow()"))
        self.assertLess(render.index("walkRow()"), render.index('el("p", { class: "footer-line" })'))
        self.assertIn('el("nav", { class: "next walk", "aria-label": "Your walk" })', self.app_js)
        self.assertIn('item.setAttribute("aria-current", "step");', self.app_js)
        self.assertIn("history.go(step - walk.at)", self.app_js)


class MoreWaysOnTest(unittest.TestCase):
    def setUp(self):
        self.graph_js = (STATIC / "graph.js").read_text()
        self.app_js = (STATIC / "app.js").read_text()

    def test_the_arrow_keys_walk_every_drawing_and_only_move_focus(self):
        walk = self.graph_js[self.graph_js.index("function walkWithKeys("):self.graph_js.index("// A node with a question asks it")]
        for key in ("ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp"):
            self.assertIn(f"{key}:", walk)
        self.assertIn("nodeEls.get(to).focus();", walk)
        self.assertNotIn("onAsk", walk)
        self.assertIn("    walkWithKeys(nodeEls, links);\n    if (meta.onAsk)", self.graph_js)

    def test_a_refusal_with_no_near_name_offers_the_example_chips_that_answer(self):
        instead = self.app_js[self.app_js.index("function insteadOf("):self.app_js.index("function chipRow(")]
        self.assertIn("examplesAnswered", instead)
        self.assertIn("examplesAnswered = answered;", self.app_js)

    def test_a_team_page_links_its_people_things_decisions_and_tickets_to_their_questions(self):
        team_js = (STATIC / "team.js").read_text()
        self.assertIn("const [question] = questionsFor(node);", team_js)
        self.assertIn("href: `/?q=${encodeURIComponent(question)}`", team_js)
        for node in ('{ name: member.name, label: "Person" }', '{ name: item.name, label: "Service", kind: item.kind }', '{ name: decision.name, label: "Decision" }', '{ name: ticket.id, label: "Ticket" }'):
            self.assertIn(f"askLink({node.split(',')[0].split(': ')[1]}, {node})", team_js)
        self.assertIn('href="/static/explore.css', (STATIC / "team.html").read_text())

    def test_a_proof_question_asks_itself(self):
        proof = self.app_js[self.app_js.index("async function loadProof()"):]
        self.assertIn('class: "proof-ask"', proof)
        self.assertIn("askFromPage(row.question)", proof)
        css = (STATIC / "explore.css").read_text()
        rule = css[css.index(".proof-ask {"):css.index("}", css.index(".proof-ask {"))]
        self.assertNotRegex(rule, r"#[0-9a-fA-F]{3,8}\b|rgba?\(")
