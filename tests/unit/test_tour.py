"""The 60-second tour (tour.js), checked without a browser.

Its steps are example chips the page already offers, it asks only through the page's hook, it
never asks more than its cap, and everything it lights up is something the page draws. The
integration test (tests/integration/test_tour_steps.py) asks each step on a fresh seed.
"""

import re
import unittest
from pathlib import Path

import tests  # noqa: F401  pins the test settings before src loads

from src.domain.knowledge import ask_api  # noqa: E402

STATIC = Path(__file__).resolve().parents[2] / "src" / "static"
STEP = re.compile(r'\{ ask: (null|"[^"]*"), spot: \[([^\]]*)\], title: "([^"]*)", text: "([^"]*)", fact: "(\w+)" \},')


def steps() -> list[dict]:
    text = (STATIC / "tour.js").read_text()
    body = re.search(r"const STEPS = \[\n(.*?)\n  \];", text, re.DOTALL).group(1)
    found = [STEP.match(line.strip()) for line in body.splitlines()]
    assert all(found), [line for line, match in zip(body.splitlines(), found) if not match]
    return [
        {"ask": None if ask == "null" else ask.strip('"'), "spot": re.findall(r'"([^"]*)"', spot), "title": title, "text": text, "fact": fact}
        for ask, spot, title, text, fact in (match.groups() for match in found)
    ]


class StepsTest(unittest.TestCase):
    def setUp(self):
        self.steps = steps()
        self.tour_js = (STATIC / "tour.js").read_text()

    def test_six_steps_in_the_order_the_brief_gives(self):
        self.assertEqual(
            [step["ask"] for step in self.steps],
            ["who owns billing, and who else has worked on it", "who knows the payments gateway", "what are our riskiest services", "who is Arjun Mehta", "what changed this week", None],
        )
        self.assertIn("#proof", self.steps[-1]["spot"])

    def test_every_question_is_an_example_chip_so_the_honesty_check_can_find_it(self):
        chips = ask_api.EXAMPLES["quillfern"]
        for step in self.steps:
            if step["ask"]:
                self.assertIn(step["ask"], chips)
        self.assertIn("offered.includes(step.ask)", self.tour_js)

    def test_the_tour_stays_under_its_ask_cap(self):
        cap = int(re.search(r"const MAX_ASKS = (\d+);", self.tour_js).group(1))
        self.assertLessEqual(cap, 8)
        self.assertLessEqual(len({step["ask"] for step in self.steps if step["ask"]}), cap)
        self.assertIn("if (tour.asks >= MAX_ASKS) return null;", self.tour_js)

    def test_six_steps_take_about_a_minute(self):
        step_ms = int(re.search(r"const STEP_MS = (\d+);", self.tour_js).group(1))
        self.assertTrue(45_000 <= step_ms * len(self.steps) <= 75_000, step_ms)

    def test_every_spot_is_something_the_page_draws(self):
        index = (STATIC / "index.html").read_text()
        drawn_by = {"canvas-frame": "graph.js", "riskmap-frame": "riskmap.js", "text": "answer.js"}
        for step in self.steps:
            for selector in step["spot"]:
                with self.subTest(selector=selector):
                    if selector.startswith("#out ."):
                        name = selector.split(".", 1)[1]
                        script = (STATIC / drawn_by[name]).read_text()
                        self.assertTrue(f'"{name}"' in script or f'"{name} ' in script, f"{drawn_by[name]} draws no .{name}")
                    else:
                        self.assertIn(f'id="{selector[1:]}"', index)

    def test_every_step_has_a_fact_the_tour_knows_how_to_read(self):
        facts = re.search(r"const FACTS = \{(.*?)\n  \};", self.tour_js, re.DOTALL).group(1)
        for step in self.steps:
            self.assertRegex(facts, rf"\n    {step['fact']}[(:]", step["fact"])


class WiringTest(unittest.TestCase):
    def setUp(self):
        self.tour_js = (STATIC / "tour.js").read_text()
        self.app_js = (STATIC / "app.js").read_text()
        self.index = (STATIC / "index.html").read_text()

    def test_the_page_hook_is_one_object_at_the_end_of_app_js(self):
        self.assertEqual(self.app_js.count("window.BrainPage = {"), 1)
        hook = self.app_js[self.app_js.index("window.BrainPage = {"):]
        self.assertIn('ask: (question) => { input.value = question; return ask(question, true, "start"); },', hook)
        self.assertIn("offered: () => toolsReady.then(() => examplesOffered.slice()),", hook)
        self.assertIn("render(data, q);\n      return data;", self.app_js)

    def test_the_tour_asks_only_through_the_page(self):
        self.assertNotIn("fetch(", self.tour_js)
        self.assertNotIn("/api/", self.tour_js)
        self.assertIn("await page.ask(question)", self.tour_js)

    def test_the_button_waits_for_the_script_and_the_page_loads_it_after_app_js(self):
        self.assertIn('<button type="button" class="link-btn tour-start" id="tour-start" hidden>60-second tour</button>', self.index)
        self.assertIn(".tour-start[hidden] { display: none; }", (STATIC / "tour.css").read_text())
        scripts = re.findall(r'<script src="/static/([a-z]+\.js)\?v=\d+"></script>', self.index)
        self.assertEqual(scripts[-2:], ["app.js", "tour.js"])

    def test_keys_motion_and_the_deep_link(self):
        self.assertIn('if (event.key === "Escape")', self.tour_js)
        self.assertIn('event.key === "ArrowRight" || event.key === "ArrowLeft"', self.tour_js)
        self.assertIn("playing: !reducedMotion()", self.tour_js)
        self.assertIn('behavior: reducedMotion() ? "auto" : "smooth"', self.tour_js)
        self.assertIn('if (params.get("tour") === "1") start(startButton, (parseInt(params.get("step"), 10) || 1) - 1);', self.tour_js)
        self.assertIn('history.replaceState(history.state, "", `?tour=1&step=${tour.at + 1}`);', self.tour_js)

    def test_the_card_is_a_labelled_dialog_that_announces_each_step(self):
        self.assertIn('role: "dialog", "aria-modal": "false", "aria-labelledby": "tour-title"', self.tour_js)
        self.assertIn('el("p", { class: "tour-text", id: "tour-text", "aria-live": "polite" })', self.tour_js)
        self.assertIn('"aria-label": `Step ${index + 1}: ${step.title}`', self.tour_js)


class PhoneTest(unittest.TestCase):
    """At phone width the card folds to one line (step, title, Next, close) and opens on a tap; Esc leaves."""

    def setUp(self):
        self.js = (STATIC / "tour.js").read_text()
        self.css = (STATIC / "tour.css").read_text()

    def test_the_card_folds_to_one_line_on_a_phone_and_opens_on_a_tap(self):
        self.assertIn('line.append(expand, button("tour-line-next", "Next", next), button("tour-close", "×", stop));', self.js)
        self.assertIn('expand.addEventListener("click", () => fold(!card.classList.contains("open")));', self.js)
        self.assertIn('line.lastChild.setAttribute("aria-label", "Close the tour");', self.js)
        self.assertIn(".tour-line { display: none;", self.css)
        phone = self.css[self.css.index("@media (max-width: 760px)"):]
        self.assertIn(".tour-line { display: flex; }", phone)
        self.assertIn(".tour-card:not(.open) > :not(.tour-line) { display: none !important; }", phone)

    def test_escape_leaves_even_while_the_tour_is_opening(self):
        self.assertIn('if (event.key === "Escape") {\n      event.preventDefault();\n      stop();', self.js)
        self.assertIn("if (!steps.length || tour || cancelled) return;", self.js)
