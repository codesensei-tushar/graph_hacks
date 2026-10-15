"""Pure memory-layer logic: tags, write requests, formatting, space names. No database."""

import unittest
from datetime import UTC, datetime

from src.domain.memory import search_brain, space_name
from src.domain.memory.tags import (
    BrainMemoryTag,
    canonicalize_proposed_tags,
    channel_brain_tag_key,
    node_label,
    normalize_brain_memory_tags,
    normalize_brain_tag_key,
    person_brain_tag_key,
    relevant_brain_tag_keys,
)
from src.domain.memory.tree import BrainTreeNode, render_brain_tree_outline
from src.domain.memory.writeback import (
    MemoryDoc,
    MemoryDocInput,
    SlackMemoryScope,
    build_slack_memory_write_request,
    memory_docs_from_writeback,
    normalize_memory_key,
    slack_memory_custom_id,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


class TagTests(unittest.TestCase):
    def test_normalize_key(self):
        self.assertEqual(normalize_brain_tag_key("  Billing Service / Invoices "), "billing_service/invoices")
        self.assertEqual(normalize_brain_tag_key("a//b"), "a/b")
        self.assertEqual(normalize_brain_tag_key("///"), "other")
        self.assertEqual(node_label("product/billing_service"), "billing service")

    def test_person_and_channel_keys(self):
        self.assertEqual(person_brain_tag_key("U123"), "person_u123")
        self.assertEqual(channel_brain_tag_key("C9"), "channel_c9")

    def test_normalize_tags_kinds_cap_and_person_allowlist(self):
        tags = normalize_brain_memory_tags(
            [
                {"key": "person_U1", "label": "Ana", "kind": "topic"},
                {"key": "person_U2", "label": "Bo", "kind": "person"},
                {"key": "channel_C1", "label": "ops", "kind": "topic"},
                {"key": "billing", "label": "Billing", "kind": "bogus"},
                {"key": "billing", "label": "dupe", "kind": "topic"},
                {"key": "a", "kind": "topic"},
                {"key": "b", "kind": "topic"},
            ],
            allowed_person_slack_user_ids=["U1"],
        )
        self.assertEqual([t.key for t in tags], ["person_u1", "channel_c1", "billing", "a"])
        self.assertEqual([t.kind for t in tags], ["person", "channel", "other", "topic"])

    def test_empty_allowlist_drops_every_person_tag(self):
        self.assertEqual(normalize_brain_memory_tags([{"key": "person_U1", "label": "x"}], allowed_person_slack_user_ids=[]), [])

    def test_canonicalize_same_kind_only(self):
        existing = [BrainMemoryTag("project_billing_service", "Billing service", "project")]
        same = canonicalize_proposed_tags(existing, [{"key": "project_billing", "label": "Billing", "kind": "project"}])
        self.assertEqual(same[0]["key"], "project_billing_service")
        other_kind = canonicalize_proposed_tags(existing, [{"key": "customer_billing", "label": "Billing", "kind": "customer"}])
        self.assertEqual(other_kind[0]["key"], "customer_billing")
        generic = canonicalize_proposed_tags(existing, [{"key": "project_status_update", "label": "x", "kind": "project"}])
        self.assertEqual(generic[0]["key"], "project_status_update")

    def test_relevant_keys_people_first_then_query(self):
        tags = [BrainMemoryTag("billing/invoices", "Invoices", "topic"), BrainMemoryTag("search", "Search", "topic")]
        self.assertEqual(relevant_brain_tag_keys(tags, "who owns invoices", ["U1"]), ["person_u1", "billing/invoices"])


class WriteRequestTests(unittest.TestCase):
    def doc(self, **kw):
        base = {"title": "Billing owner", "content": "Maya owns the billing service.", "tags": [{"key": "billing", "label": "Billing", "kind": "topic"}]}
        base.update(kw)
        return MemoryDocInput.from_doc(base)

    def test_request_shape(self):
        request = build_slack_memory_write_request(self.doc(event_date="2026-10-01"), SlackMemoryScope(kind="shared", channel_id="C1"), NOW)
        self.assertEqual(request.container_tag, "org_shared")
        self.assertTrue(request.content.startswith("DOCUMENT_DATE: 2026-10-15\n"))
        self.assertEqual(request.memory, "Maya owns the billing service. (as of 2026-10-01)")
        self.assertEqual(request.metadata["brain_tags"], ["billing"])
        self.assertEqual(request.metadata["slack_channel_id"], "C1")
        self.assertTrue(request.custom_id.startswith("company-brain-slack:2026-10-15:"))

    def test_custom_id_is_stable_and_epoch_scoped(self):
        scope = SlackMemoryScope(kind="shared")
        a = build_slack_memory_write_request(self.doc(), scope, NOW, expected_reset_epoch=1)
        b = build_slack_memory_write_request(self.doc(), scope, NOW, expected_reset_epoch=1)
        c = build_slack_memory_write_request(self.doc(), scope, NOW, expected_reset_epoch=2)
        self.assertEqual(a.custom_id, b.custom_id)
        self.assertNotEqual(a.custom_id, c.custom_id)

    def test_channel_tags_rewrite_to_the_scope_channel(self):
        doc = self.doc(tags=[{"key": "channel_wrong", "label": "ops", "kind": "channel"}, {"key": "channel_other", "label": "x", "kind": "channel"}])
        request = build_slack_memory_write_request(doc, SlackMemoryScope(kind="private_channel", channel_id="G7"), NOW)
        self.assertEqual([t.key for t in request.tags], ["channel_g7"])
        self.assertEqual(request.container_tag, "slack_channel_G7")

    def test_dm_without_user_writes_nothing_and_empty_content_is_dropped(self):
        self.assertIsNone(build_slack_memory_write_request(self.doc(), SlackMemoryScope(kind="dm"), NOW))
        self.assertIsNone(build_slack_memory_write_request(self.doc(content="  "), SlackMemoryScope(kind="shared"), NOW))

    def test_observe_ids_pass_through(self):
        doc = self.doc(internal_custom_id_override="company-brain-channel-observe:abc")
        self.assertEqual(build_slack_memory_write_request(doc, SlackMemoryScope(kind="shared"), NOW).custom_id, "company-brain-channel-observe:abc")

    def test_docs_from_writeback_and_schema(self):
        self.assertEqual(memory_docs_from_writeback(None), [])
        docs = memory_docs_from_writeback([{"title": "t", "content": "x"}, {"title": "t", "content": " "}])
        self.assertEqual(len(docs), 1)
        with self.assertRaises(ValueError):
            MemoryDoc(title="t", content="c", tags=[])
        self.assertEqual(normalize_memory_key("Billing Owner!"), "billing-owner")
        self.assertTrue(slack_memory_custom_id("2026-01-01", "k", "raw").startswith("company-brain-slack:2026-01-01:"))


class FormatTests(unittest.TestCase):
    def test_similarity_and_terms(self):
        self.assertEqual(search_brain.query_terms("Who owns the billing service?"), ["owns", "billing", "service"])
        self.assertAlmostEqual(search_brain.similarity("billing services", "the billing service is down"), 1.0)
        self.assertEqual(search_brain.similarity("the and", "anything"), 0.0)

    def test_format_results(self):
        results = [{"id": "m1", "memory": "a  b", "similarity": 0.9}, {"id": "m2", "memory": "low", "similarity": 0.1}, {"memory": ""}]
        formatted = search_brain.format_brain_results(results)
        self.assertEqual(formatted, "1. Memory (90%) m1: a b")
        self.assertEqual(search_brain.count_brain_results(formatted), 1)
        self.assertEqual(search_brain.format_brain_results([]), "(none found)")
        self.assertEqual(search_brain.brain_card_title(1), "Found 1 relevant memory")
        self.assertEqual(search_brain.brain_card_title(0), "No relevant memories found")
        self.assertEqual(search_brain.format_brain_score_summary(results[:1]), "0.900")

    def test_render_outline(self):
        nodes = [BrainTreeNode("a", "A", 0, 3, [BrainTreeNode(f"a/{i}", f"c{i}", 1, 1) for i in range(8)])]
        self.assertEqual(render_brain_tree_outline(nodes), "- A › c0, c1, c2, c3, c4, c5, +2")
        self.assertEqual(render_brain_tree_outline([]), "")


class SpaceNameTests(unittest.TestCase):
    def test_space_names(self):
        self.assertEqual(space_name.slack_channel_space_name("G1", "ops"), "slack_ops")
        self.assertEqual(space_name.slack_channel_space_name("G1"), "slack_G1")
        self.assertTrue(space_name.is_generated_brain_space_name(None, "slack_channel_G1"))
        self.assertTrue(space_name.is_generated_brain_space_name("#G1", "slack_channel_G1"))
        self.assertTrue(space_name.is_generated_brain_space_name("#ops", "slack_channel_G1", "slack_ops"))
        self.assertFalse(space_name.is_generated_brain_space_name("Ops room", "slack_channel_G1"))
        self.assertTrue(space_name.is_slack_managed_brain_space_name("x", "slack_channel_G1", "slack_ops", {"slack_managed_space_name": "x"}))


if __name__ == "__main__":
    unittest.main()
