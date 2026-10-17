"""Decisions that stay current: record one, trace one, and what each asker may see.

Runs on the fictional Quillfern company in a throwaway graph. Needs FalkorDB on
localhost:6379 (``docker compose up -d``).
"""

import re
import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client
from src.domain.knowledge import catalogue, decisions
from src.domain.memory import memory_service
from src.domain.slack import compute
from src.seed import quillfern, seed

GRAPH = f"company_decisions_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
ROOM = "slack_channel_GPAYMENTS"
NEW = "Move billing retries to the invoice worker"
OLD = "Retry renewal webhooks inside the billing service"
WHEN = 1_792_237_200_000  # 17 Oct 2026 11:30 UTC


def _link_slack_user(slack_user_id: str, person_id: str) -> None:
    _, graph = falkordb_client.connect()
    graph.query("MERGE (u:SlackUser {id: $u}) WITH u MATCH (p:Person {id: $p}) MERGE (u)-[:IS]->(p)", {"u": slack_user_id, "p": person_id})


class DecisionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def setUp(self):
        seed.reset_and_seed(quillfern)
        _link_slack_user("UARJUN", "arjun")

    def _record(self, **overrides) -> dict:
        args = {"title": "move billing retries to the invoice worker", "asker_slack_user_id": "UARJUN", "channel_id": "CENG", "decided_at": WHEN, "scope": "org_shared"}
        args.update(overrides)
        return catalogue.get_tool("record_decision").run(args, ORG)

    # --- the seed ---------------------------------------------------------------------------

    def test_seeded_trace_shows_the_current_decision_and_what_it_replaced(self):
        result = decisions.decision_trace("why did we move the memory layer off postgres", ORG)
        self.assertTrue(result.found)
        self.assertTrue(result.answer.startswith("Move the memory layer off Postgres"), result.answer)
        self.assertIn("Decided 15 Sep 2026.", result.answer)
        self.assertIn("Based on: RFC: memory layer on a graph store, TKT-101.", result.answer)
        self.assertIn("This replaced *Keep notebook links in Postgres join tables* (decided 1 Jun 2026)", result.answer)
        cites = {(step.from_name, step.rel, step.to_name) for step in result.path}
        self.assertIn(("Move the memory layer off Postgres", "SUPERSEDES", "Keep notebook links in Postgres join tables"), cites)
        self.assertIn(("Move the memory layer off Postgres", "BASED_ON", "TKT-101"), cites)
        self.assertIn(("Sara Lindqvist", "PROPOSED", "Keep notebook links in Postgres join tables"), cites)

    def test_asking_about_a_stale_decision_answers_with_the_current_one(self):
        result = decisions.decision_trace("why do we keep notebook links in postgres join tables", ORG)
        self.assertTrue(result.found)
        self.assertTrue(result.answer.startswith("*Keep notebook links in Postgres join tables* (decided 1 Jun 2026) is no longer current. Move the memory layer off Postgres."), result.answer)

    def test_a_decision_nobody_made_is_a_miss_with_candidates(self):
        result = decisions.decision_trace("why did we rewrite the espresso machine firmware", ORG)
        self.assertFalse(result.found)
        self.assertEqual(result.path, [])

    # --- recording -----------------------------------------------------------------------------

    def test_record_supersedes_the_standing_decision_about_the_same_thing(self):
        result = self._record()
        self.assertEqual(result.tool, "record_decision")
        self.assertIn(f"Recorded *{NEW}* about Billing Service, Invoice Worker, replacing *{OLD}* (decided 20 Sep 2026).", result.answer)
        self.assertIn("Decided 17 Oct 2026.", result.answer)
        self.assertIn("Proposed by Arjun Mehta.", result.answer)
        self.assertIn("Decided in #CENG.", result.answer)
        steps = {(step.from_name, step.rel, step.to_name) for step in result.path}
        for step in (("Arjun Mehta", "PROPOSED", NEW), (NEW, "ABOUT", "Billing Service"), (NEW, "SUPERSEDES", OLD), (NEW, "DECIDED_IN", "#CENG")):
            self.assertIn(step, steps)

        trace = decisions.decision_trace("why did we move billing retries to the invoice worker", ORG)
        self.assertTrue(trace.answer.startswith(f"{NEW}. Decided 17 Oct 2026."), trace.answer)
        self.assertIn(f"This replaced *{OLD}* (decided 20 Sep 2026)", trace.answer)
        stale = decisions.decision_trace("where do billing retries live", ORG)
        self.assertTrue(stale.answer.startswith(f"*{OLD}* (decided 20 Sep 2026) is no longer current. {NEW}."), stale.answer)

    def test_record_with_explicit_supersedes_and_sources(self):
        result = self._record(
            title="retire the invoice format spec",
            about="invoice worker",
            supersedes="memory layer off postgres",
            sources=["TKT-102", "Billing runbook"],
            summary="One fewer document to keep current.",
        )
        steps = {(step.from_name, step.rel, step.to_name) for step in result.path}
        self.assertIn(("Retire the invoice format spec", "SUPERSEDES", "Move the memory layer off Postgres"), steps)
        self.assertIn(("Retire the invoice format spec", "BASED_ON", "TKT-102"), steps)
        self.assertIn(("Retire the invoice format spec", "BASED_ON", "Billing runbook"), steps)
        self.assertIn("Based on: Billing runbook, TKT-102.", result.answer)

    def test_record_without_a_matching_service_links_nothing_and_says_so(self):
        preview = catalogue.get_tool("record_decision").preview({"title": "paint the office green", "decided_at": WHEN}, ORG)
        self.assertIn("not linked to any service", preview)
        result = self._record(title="paint the office green", asker_slack_user_id="UNOBODY")
        self.assertTrue(result.answer.startswith("Recorded *Paint the office green*, replacing nothing."), result.answer)
        self.assertIn("<@UNOBODY>, who is not linked to a person", result.answer)
        self.assertEqual([s for s in result.path if s.rel in ("ABOUT", "SUPERSEDES", "PROPOSED")], [])

    def test_a_decision_about_the_same_service_but_another_thing_replaces_nothing(self):
        preview = catalogue.get_tool("record_decision").preview({"title": "charge annual plans in local currency", "about": "billing", "decided_at": WHEN}, ORG)
        self.assertTrue(preview.endswith(", replacing nothing"), preview)
        result = self._record(title="charge annual plans in local currency", about="billing")
        self.assertEqual([s for s in result.path if s.rel == "SUPERSEDES"], [])
        self.assertIn("replacing nothing", result.answer)

    def test_a_named_decision_that_is_not_in_the_graph_is_refused_not_guessed(self):
        preview = catalogue.get_tool("record_decision").preview({"title": "move billing retries to the invoice worker", "supersedes": "the espresso rota", "decided_at": WHEN}, ORG)
        self.assertIn("replacing *the espresso rota*, which is not in the graph", preview)
        result = self._record(supersedes="the espresso rota")
        self.assertFalse(result.found)
        self.assertTrue(result.answer.startswith("Not recorded:"), result.answer)
        _, graph = falkordb_client.connect()
        self.assertEqual(graph.query("MATCH (d:Decision) WHERE d.name STARTS WITH 'Move billing' RETURN count(d)").result_set[0][0], 0)

    def test_a_decision_quotes_the_message_it_was_recorded_from(self):
        said = "we're moving billing retries to the invoice worker, decided in today's sync"
        memory_service.write(said, "org_shared", "UARJUN", "CENG", f"{WHEN / 1000 - 20:.6f}")
        _, graph = falkordb_client.connect()
        graph.query("MERGE (c:Channel {id: 'CENG'}) SET c.name = 'engineering'")
        result = self._record()
        self.assertIn(f"Decided in #engineering on 17 Oct 2026 by Arjun Mehta: “{said}”.", result.answer)
        self.assertNotIn("Decided 17 Oct 2026.", result.answer, "the quote sentence carries the date")
        steps = {(step.from_name, step.rel, step.to_name) for step in result.path}
        self.assertIn((NEW, "RECORDED_FROM", f"“{said}”"), steps)
        trace = decisions.decision_trace("why did we move billing retries to the invoice worker", ORG)
        self.assertIn(f"Decided in #engineering on 17 Oct 2026 by Arjun Mehta: “{said}”.", trace.answer)
        self.assertIn((NEW, "RECORDED_FROM", f"“{said}”"), {(s.from_name, s.rel, s.to_name) for s in trace.path})

    def test_the_source_is_found_when_it_is_seconds_older_than_the_decision(self):
        said = "moving billing retries to the invoice worker"
        memory_service.write(said, "org_shared", "UARJUN", "CENG", f"{WHEN / 1000 - 0.03:.6f}")
        result = self._record(decided_at=WHEN + 1)
        self.assertIn("RECORDED_FROM", {step.rel for step in result.path}, result.answer)
        self.assertIn(f"“{said}”", result.answer)

    def test_slack_ts_compare_as_strings_only_in_the_fixed_format(self):
        # source_message compares ts as strings. That orders like the numbers only while both
        # sides have ten-digit seconds and six-digit micros, which holds for real Slack ts
        # (until 2286). Pin the format so a change on either side fails here, not silently.
        slack_ts = re.compile(r"^\d{10}\.\d{6}$")
        bounds = [decisions._slack_ts(WHEN / 1000 + 1), decisions._slack_ts(WHEN / 1000 - decisions.SOURCE_WINDOW_SECONDS)]
        for bound in bounds:
            self.assertRegex(bound, slack_ts)
        memory_service.write("moving billing retries to the invoice worker", "org_shared", "UARJUN", "CENG", "1792237199.970000")
        _, graph = falkordb_client.connect()
        for (ts,) in graph.query("MATCH (m:Memory) WHERE m.ts IS NOT NULL RETURN m.ts").result_set:
            self.assertRegex(ts, slack_ts)
        samples = ["1792237199.970000", "1792237200.000100", "1792237201.000000", "1792236300.000000", "1792237199.999999"]
        self.assertEqual(sorted(samples), sorted(samples, key=float))

    def test_an_old_or_someone_elses_message_is_not_the_source(self):
        memory_service.write("billing alerts are noisy again", "org_shared", "UARJUN", "CENG", f"{WHEN / 1000 - 3600:.6f}")
        memory_service.write("lunch is at one today", "org_shared", "ULENA", "CENG", f"{WHEN / 1000 - 5:.6f}")
        result = self._record()
        self.assertNotIn("RECORDED_FROM", {step.rel for step in result.path})
        self.assertIn("Decided 17 Oct 2026. Proposed by Arjun Mehta.", result.answer)

    def test_a_decision_recorded_in_a_private_room_stays_there(self):
        self._record(scope=ROOM, channel_id="GPAYMENTS")
        outsider = decisions.decision_trace("why did we move billing retries to the invoice worker", ORG)
        self.assertTrue(outsider.answer.startswith(OLD), "from outside the room the old decision still stands")
        insider = decisions.decision_trace("why did we move billing retries to the invoice worker", ORG + [ROOM])
        self.assertTrue(insider.answer.startswith(NEW), insider.answer)

    def test_decided_at_accepts_seconds_and_milliseconds(self):
        self.assertEqual(decisions.to_millis(1_792_237_200), WHEN)
        self.assertEqual(decisions.to_millis("1792237200.000100"), WHEN)
        self.assertEqual(decisions.to_millis(WHEN), WHEN)
        self.assertIsNone(decisions.to_millis("soon"))
        self.assertEqual(decisions.format_date(WHEN), "17 Oct 2026")

    # --- the no-model command ----------------------------------------------------------------

    def test_decision_command_suspends_for_approval_with_a_resolved_preview(self):
        self.assertIsNotNone(compute._DECISION.match("decision: move billing retries to the invoice worker"))
        self.assertIsNotNone(compute._DECISION.match("we decided - move billing retries"))
        self.assertIsNone(compute._DECISION.match("why did we decide to move billing retries"))
        spec = catalogue.get_tool("record_decision")
        self.assertTrue(spec.needs_approval)
        preview = spec.preview({"title": "move billing retries to the invoice worker", "decided_at": WHEN}, ORG)
        self.assertEqual(preview, f"Record the decision *{NEW}* about *Billing Service, Invoice Worker*, replacing *{OLD}* (decided 20 Sep 2026)")
        self.assertEqual(compute._epoch_millis("1792237200.000100"), WHEN)
        self.assertIsNone(compute._epoch_millis(None))


if __name__ == "__main__":
    unittest.main()
