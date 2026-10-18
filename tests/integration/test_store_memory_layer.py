"""Memory layer on a real FalkorDB (throwaway graph ``memory_test``).

Needs FalkorDB on localhost:6379 (``docker compose up -d``). Named to sort after
test_slack_permissions: settings are read once at first import, and that test sets
SLACK_OWNER_ID before importing src.
"""

import os
import unittest
import uuid
from unittest import mock

os.environ["FALKORDB_GRAPH"] = "memory_test" + os.environ.get("TEST_GRAPH_SUFFIX", "")

from src.db.falkordb_client import connect  # noqa: E402
from src.domain.memory import documents, memory_service, memory_tools, tree  # noqa: E402
from src.domain.memory.company_context import get_company_context, write_company_context  # noqa: E402
from src.domain.memory.containers import AGENT_SELF_CONTAINER_TAG, get_container_tag_settings, update_container_tag_settings  # noqa: E402
from src.domain.memory.entities import _write_entity, resolve_entity, ResolvedEntity  # noqa: E402
from src.domain.memory.interaction_style import load_interaction_style_profile, render_interaction_style  # noqa: E402
from src.domain.memory.profile_recall import BrainProfileRecallInput, build_ambient_brain_profile_context, build_brain_profile_context  # noqa: E402
from src.domain.memory.profile_sync import BrainProfileSyncContext, clear_brain_profile_sync, maybe_sync_brain_profile_config  # noqa: E402
from src.domain.memory.search_brain import search_brain  # noqa: E402
from src.domain.memory.split import SPLIT_THRESHOLD, maybe_split_node  # noqa: E402
from src.domain.memory.tags import list_brain_memory_tags  # noqa: E402
from src.domain.memory.workspace_prompt import MAX_WORKSPACE_PROMPT_LENGTH, get_workspace_prompt, set_workspace_prompt  # noqa: E402
from src.domain.memory.writeback import MemoryDocInput, SlackMemoryScope  # noqa: E402
from src.domain.slack.channel_membership import record_membership  # noqa: E402

SHARED = SlackMemoryScope(kind="shared", channel_id="CPUB")


def doc(content, tags, title=None, buckets=None):
    return MemoryDocInput(title=title or content[:20], content=content, tags=tags, buckets=buckets or [])


class MemoryLayerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The reset epoch is one Redis key that every checkout's suite shares; another run's reset
        # advancing it between a read and a write made those writes refuse. This class gets its own.
        cls._epoch = mock.patch.object(tree, "_RESET_EPOCH_KEY", f"brain:memory:reset_epoch:test{os.environ.get('TEST_GRAPH_SUFFIX', '')}")
        cls._epoch.start()
        memory_service.ensure_indexes()

    @classmethod
    def tearDownClass(cls):
        try:
            _, graph = connect()
            graph.delete()
        finally:
            cls._epoch.stop()

    def setUp(self):
        _, graph = connect()
        graph.query("MATCH (n) WHERE n:Memory OR n:Tag OR n:Scope DETACH DELETE n")
        self.run_id = uuid.uuid4().hex[:6]

    def count(self, cypher, params=None):
        _, graph = connect()
        return graph.query(cypher, params or {}).result_set[0][0]

    # --- writes ----------------------------------------------------------------------

    def test_write_memory_registers_tags_files_topics_and_upserts(self):
        tags = [{"key": "product/billing", "label": "Billing", "kind": "topic"}, {"key": "person_U1", "label": "Maya", "kind": "person"}]
        first = memory_service.write_memory(doc("Maya owns the billing service and its invoices.", tags), SHARED, split_in_background=False)
        again = memory_service.write_memory(doc("Maya owns the billing service and its invoices.", tags), SHARED, split_in_background=False)
        self.assertTrue(first["written"])
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(self.count("MATCH (m:Memory) RETURN count(m)"), 1)
        self.assertEqual(self.count("MATCH (m:Memory) RETURN m.source_count"), 2)
        keys = {t.key for t in list_brain_memory_tags(["org_shared"])}
        self.assertEqual(keys, {"product/billing", "person_u1"})  # implicit parent "product" is not listed
        self.assertEqual(self.count("MATCH (:Tag {key: 'product/billing'})-[:PARENT]->(:Tag {key: 'product'}) RETURN count(*)"), 1)
        self.assertEqual(self.count("MATCH (:Memory)-[:FILED_UNDER]->(t:Tag) RETURN count(t)"), 1)  # person tags aren't topic nodes

    def test_tags_canonicalize_onto_existing_container_tags(self):
        memory_service.write_memory(doc("Billing service runbook lives in the wiki.", [{"key": "project_billing_service", "label": "Billing service", "kind": "project"}]), SHARED)
        memory_service.write_memory(doc("Billing retries are capped at five.", [{"key": "project_billing", "label": "Billing", "kind": "project"}]), SHARED)
        self.assertEqual([t.key for t in list_brain_memory_tags(["org_shared"])], ["project_billing_service"])

    def test_untagged_or_stale_writes_are_refused(self):
        self.assertFalse(memory_service.write_memory(doc("No tags at all here.", []), SHARED)["written"])
        epoch = tree.get_brain_memory_reset_epoch()
        tree.advance_brain_memory_reset_epoch()
        stale = memory_service.write_memory(doc("Written from before the reset.", [{"key": "x_topic", "kind": "topic"}]), SHARED, expected_reset_epoch=epoch)
        self.assertFalse(stale["written"])
        self.assertEqual(self.count("MATCH (m:Memory) RETURN count(m)"), 0)

    def test_reset_registry_clears_tags(self):
        memory_service.write_memory(doc("Search reindex runs nightly.", [{"key": "search", "kind": "topic"}]), SHARED)
        before = tree.get_brain_memory_reset_epoch()
        self.assertEqual(memory_service.reset_memory_registry(), before + 1)
        self.assertEqual(list_brain_memory_tags(["org_shared"]), [])

    # --- reads -----------------------------------------------------------------------

    def test_search_brain_respects_the_read_surface(self):
        private = f"GPRIV{self.run_id}"
        member, outsider = f"UMEM{self.run_id}", f"UOUT{self.run_id}"
        memory_service.write_memory(doc("Invoice worker backlog causes billing retries.", [{"key": "billing", "kind": "topic"}]), SlackMemoryScope(kind="private_channel", channel_id=private))
        memory_service.write_memory(doc("Billing dashboards moved to the metrics board.", [{"key": "billing", "kind": "topic"}]), SHARED)
        record_membership(private, member)

        def texts(scope):
            return " | ".join(r["memory"] for r in search_brain("billing retries dashboards", scope)["results"])

        self.assertNotIn("backlog", texts(SHARED))
        self.assertIn("backlog", texts(SlackMemoryScope(kind="private_channel", channel_id=private)))
        self.assertIn("backlog", texts(SlackMemoryScope(kind="dm", user_id=member, slack_user_id=member)))
        self.assertNotIn("backlog", texts(SlackMemoryScope(kind="dm", user_id=outsider, slack_user_id=outsider)))
        self.assertIn("metrics board", texts(SlackMemoryScope(kind="dm", user_id=outsider, slack_user_id=outsider)))

    def test_legacy_write_and_search_shape(self):
        memory_service.write(f"the auth service token cache expires hourly {self.run_id}", "org_shared", "U9", "C1", "1.1")
        results = memory_service.search("auth token cache", ["org_shared"])
        self.assertEqual(len(results), 1)
        self.assertEqual({"text", "scope", "channel_id", "ts", "author_id", "author"} <= set(results[0]), True)
        self.assertEqual(results[0]["author_id"], "U9")

    def test_outline_and_read_memory_node(self):
        for i in range(3):
            memory_service.write_memory(doc(f"Invoice note {i} about late fees.", [{"key": "billing/invoices", "label": "Invoices", "kind": "topic"}]), SHARED)
        memory_service.write_memory(doc("Refunds need a second approver.", [{"key": "billing/refunds", "label": "Refunds", "kind": "topic"}]), SHARED)
        outline = memory_tools.outline_memory_tree(SHARED)["tree"]
        self.assertEqual(outline[0]["path"], "billing")
        self.assertEqual({c["path"] for c in outline[0]["children"]}, {"billing/invoices", "billing/refunds"})
        node = memory_tools.read_memory_node("billing", SHARED)
        self.assertEqual(len(node["memories"]), 4)
        self.assertEqual(len(memory_tools.read_memory_node("billing/refunds", SHARED)["memories"]), 1)
        self.assertIn("note", memory_tools.read_memory_node("billing", SHARED, before="not-a-date"))

    def test_forget_previews_then_forgets_exactly_the_previewed_ids(self):
        memory_service.write_memory(doc("Old pricing model had three tiers.", [{"key": "pricing", "kind": "topic"}]), SHARED)
        memory_service.write_memory(doc("Deploys happen on Tuesdays.", [{"key": "deploys", "kind": "topic"}]), SHARED)
        self.assertIn("error", memory_tools.forget_memories("pricing", None))
        self.assertIn("error", memory_tools.forget_memories("pricing", SHARED, dry_run=False))
        preview = memory_tools.forget_memories("old pricing model", SHARED)
        self.assertEqual(preview["count"], 1)
        applied = memory_tools.forget_memories("old pricing model", SHARED, dry_run=False, ids=preview["ids"])
        self.assertEqual(applied["forgottenCount"], 1)
        self.assertEqual(search_brain("old pricing model", SHARED)["results"], [])
        self.assertEqual(len(search_brain("deploys tuesdays", SHARED)["results"]), 1)

    def test_profile_recall_and_ambient_profile(self):
        memory_service.write_memory(doc("Maya prefers async updates over meetings.", [{"key": "person_UMAYA", "kind": "person"}], buckets=["preferences"]), SHARED)
        memory_service.write_memory(doc("The billing team ships every Thursday.", [{"key": "billing", "kind": "topic"}]), SHARED)
        recalled = build_brain_profile_context(BrainProfileRecallInput(query="billing", scope=SHARED))
        self.assertIn("ships every Thursday", recalled)
        ambient = build_ambient_brain_profile_context(BrainProfileRecallInput(sender_slack_user_id="UMAYA", scope=SHARED))
        self.assertIn("Profile buckets for asker (Slack UMAYA)", ambient)
        self.assertIn("preferences:", ambient)
        self.assertIn("Static profile:", ambient)
        self.assertEqual(memory_tools.recall_tagged_memories("zzz", SlackMemoryScope(kind="dm"))["found"], True)  # outline still present

    def test_interaction_style_from_agent_self(self):
        documents.upsert_brain_document(content="Keep replies short.", container_tag=AGENT_SELF_CONTAINER_TAG, buckets=["do_not"])
        documents.upsert_brain_document(content="Dry humor lands here.", container_tag=AGENT_SELF_CONTAINER_TAG, buckets=["social"])
        rendered = render_interaction_style(load_interaction_style_profile("U1"))
        self.assertIn("Avoid:\n- Keep replies short.", rendered)
        self.assertIn("Social", rendered)

    # --- settings ---------------------------------------------------------------------

    def test_profile_sync_names_containers_but_keeps_renames(self):
        clear_brain_profile_sync()
        update_container_tag_settings("org_shared", name="Our Brain")
        maybe_sync_brain_profile_config(BrainProfileSyncContext(org_name="Quillfern", asker_user_id="U1", private_channel_id="G1", private_channel_name="ops"))
        self.assertEqual(get_container_tag_settings("org_shared").name, "Our Brain")
        self.assertIn("Quillfern", get_container_tag_settings("org_shared").entity_context)
        self.assertEqual(get_container_tag_settings("user_U1").name, "My Brain")
        self.assertEqual(get_container_tag_settings("slack_channel_G1").name, "slack_ops")
        self.assertEqual([b.key for b in get_container_tag_settings("agent_self").profile_buckets][0], "voice")
        clear_brain_profile_sync()

    def test_workspace_prompt(self):
        previous = get_workspace_prompt()
        try:
            self.assertIsNone(set_workspace_prompt("   "))
            self.assertEqual(len(set_workspace_prompt("x" * 5000)), MAX_WORKSPACE_PROMPT_LENGTH)
            self.assertEqual(set_workspace_prompt(" be terse "), "be terse")
            self.assertEqual(get_workspace_prompt(), "be terse")
        finally:
            set_workspace_prompt(previous)

    def test_company_context_upserts_once(self):
        self.assertEqual(write_company_context("org1", "Quillfern", about=""), {"written": False})
        write_company_context("org1", "Quillfern", "quillfern.example.com", "Team notebooks.")
        write_company_context("org1", "Quillfern", "quillfern.example.com", "Team notebooks for small teams.")
        self.assertIn("small teams", get_company_context())
        self.assertEqual(self.count("MATCH (m:Memory {type: 'company_context'}) RETURN count(m)"), 1)

    def test_entities_resolve_from_brain_and_skip_web_without_model(self):
        self.assertIsNone(resolve_entity("org1", "Nobody Corp"))
        _write_entity("org1", ResolvedEntity("Harbor Tools", "harbor.example.com", ["Harbor"]), tree.get_brain_memory_reset_epoch())
        found = resolve_entity("org1", "harbor")
        self.assertEqual(found.canonical, "Harbor Tools")
        self.assertEqual(found.source, "brain")

    def test_export_and_overview(self):
        memory_service.write_memory(doc("Shared fact about onboarding.", [{"key": "onboarding", "kind": "topic"}]), SHARED)
        memory_service.write_memory(doc("Personal note about my week.", [{"key": "week", "kind": "task"}]), SlackMemoryScope(kind="dm", user_id="U1", slack_user_id="U1"))
        overview = documents.brain_memories_overview("U1")
        self.assertEqual(overview["count"], 2)
        self.assertEqual(documents.brain_memories_overview("U2")["count"], 1)
        filename, markdown = documents.export_brain_memories("U1", "Quillfern")
        self.assertTrue(filename.startswith("company-brain-memories-"))
        self.assertIn("## Only you", markdown)
        self.assertIn("Personal note about my week.", markdown)
        page = documents.brain_graph_page("U2")
        self.assertEqual(page["pagination"]["totalItems"], 1)

    def test_split_without_model_keeps_the_node_whole(self):
        epoch = tree.get_brain_memory_reset_epoch()
        for i in range(SPLIT_THRESHOLD):
            result = documents.upsert_brain_document(content=f"Search note {i}", container_tag="org_shared", custom_id=f"s{i}")
            self.assertTrue(tree.upsert_brain_memory_node(tree.BrainNodeMapping(result.id, "org_shared", "search"), epoch))
        maybe_split_node("org_shared", "search", epoch)
        self.assertEqual(len(tree.list_brain_memory_node_mappings(["org_shared"], "search", descendants=False)), SPLIT_THRESHOLD)

if __name__ == "__main__":
    unittest.main()
