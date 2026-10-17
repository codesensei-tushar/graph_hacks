"""Memory tools on a real FalkorDB: memories are dated nodes tied to the graph by MENTIONS edges."""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import ask_service, catalogue, memories  # noqa: E402
from src.domain.memory import memory_service  # noqa: E402
from src.seed import quillfern, seed  # noqa: E402

GRAPH = f"company_memories_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
PRIVATE = "slack_channel_GPRIV1"


class MemoryToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        memory_service.write("billing retries now go through the invoice worker, decided in sync", "org_shared", "UBO", "CPUB", "1700000100.000100")
        memory_service.write("auth session refresh bug is back after the rotation change", "org_shared", "UBO", "CPUB", "1700000200.000100")
        memory_service.write("private: billing export job was moved to the night window", PRIVATE, "UAL", "GPRIV1", "1700000300.000100")

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def test_related_memories_expand_one_hop_and_respect_the_read_set(self):
        public = memories.related_memories("billing", ORG)
        self.assertTrue(public.found)
        texts = [memory.text for memory in public.memories]
        self.assertEqual(len(texts), 2)
        self.assertTrue(texts[0].startswith("billing retries"))
        self.assertTrue(texts[1].startswith("auth session"))
        self.assertIn("2 memories you can read touch Billing Service: 1 mentions it directly, 1 via Auth Service.", public.answer)
        self.assertTrue(all(memory.learned_at for memory in public.memories))
        cites = [step.cite() for step in public.path]
        self.assertIn("Billing Service -[DEPENDS_ON]-> Auth Service", cites)
        self.assertTrue(any(cite.endswith("-[MENTIONS]-> Billing Service") for cite in cites))
        self.assertTrue(any(cite.endswith("-[MENTIONS]-> Auth Service") for cite in cites))

        private = memories.related_memories("billing", ORG + [PRIVATE])
        self.assertEqual(len(private.memories), 3)
        self.assertIn("night window", " ".join(memory.text for memory in private.memories))
        self.assertEqual([memory.scope for memory in private.memories if "night window" in memory.text], [PRIVATE])

    def test_related_memories_with_nothing_direct_and_with_nothing_at_all(self):
        postgres = memories.related_memories("postgres", ORG)
        self.assertIn("0 mention it directly, 2 via Auth Service and Billing Service", postgres.answer)
        quiet = memories.related_memories("graph store", ORG)
        self.assertEqual(quiet.answer, "No memory you can read mentions Graph Store or anything one hop from it.")
        miss = memories.related_memories("the espresso machine", ORG)
        self.assertFalse(miss.found)

    def test_recall_returns_dated_lines_and_no_path(self):
        found = memories.recall("billing retries", ORG)
        self.assertEqual(found.path, [])
        self.assertEqual(len(found.memories), 1)
        self.assertTrue(found.memories[0].learned_at)
        self.assertRegex(found.answer, r"^1 memory match\. Most recent: \d{4}-\d{2}-\d{2}\.$")
        nothing = memories.recall("espresso machine descaling", ORG)
        self.assertEqual(nothing.answer, "Nothing in the memories I can read mentions “espresso machine descaling”.")
        self.assertEqual(nothing.memories, [])

    def test_both_tools_run_through_the_catalogue_and_the_router(self):
        self.assertEqual(catalogue.get_tool("related_memories").run({"name": "billing", "k": 1}, ORG).memories.__len__(), 1)
        self.assertEqual(catalogue.get_tool("recall").run({"query": "rotation change"}, ORG).tool, "recall")
        self.assertEqual(ask_service.ask("what have we said about billing", ORG).tool, "related_memories")
        self.assertEqual(ask_service.ask("who said the retries were flaky", ORG).tool, "recall")
        self.assertIn("memories", ask_service.ask("what have we said about billing", ORG).to_dict())
