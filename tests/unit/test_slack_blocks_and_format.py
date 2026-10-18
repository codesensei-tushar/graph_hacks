import unittest
from unittest import mock

from src.connectors import registry
from src.domain.slack.format import format_answer, format_scopes
from src.integrations.slack.client import (
    APPROVAL_APPROVE_ACTION_ID,
    APPROVAL_DENY_ACTION_ID,
    SlackApprovalCard,
    approval_blocks,
    resolved_approval_blocks,
)

CARD = SlackApprovalCard(approval_id="ap1", summary="Create a ticket", tool_name="Create ticket", asker_user="U1", expires_at=1_700_000_900)


def _actions(blocks):
    return [b for b in blocks if b["type"] == "actions"]


class ApprovalBlockTests(unittest.TestCase):
    def test_pending_has_buttons(self):
        blocks = approval_blocks(CARD)
        actions = _actions(blocks)
        self.assertEqual(len(actions), 1)
        buttons = actions[0]["elements"]
        self.assertEqual([b["action_id"] for b in buttons], [APPROVAL_APPROVE_ACTION_ID, APPROVAL_DENY_ACTION_ID])
        self.assertTrue(all(b["value"] == "ap1" for b in buttons))
        self.assertIn("needs approval", blocks[0]["text"]["text"])
        self.assertIn("<!date^1700000900^", str(blocks))

    def test_resolved_has_no_buttons(self):
        for status in ("Approved", "Denied", "Expired", "Cancelled"):
            with self.subTest(status=status):
                blocks = resolved_approval_blocks(CARD, status)
                self.assertEqual(_actions(blocks), [])
                self.assertIn(status.lower(), blocks[0]["text"]["text"])
                self.assertIn("Requested by <@U1>", str(blocks))


class FormatTests(unittest.TestCase):
    def test_answer_with_path_and_memories(self):
        result = {"tool": "who_knows", "answer": "Billing is owned by A.", "path": [{"from": "A", "rel": "OWNS", "to": "Billing"}]}
        memories = [{"text": "billing retries are flaky", "scope": "slack_channel_G1", "channel_id": "G1", "author": "Bo"}]
        text = format_answer(result, memories, ["org_shared", "slack_channel_G1"], {"G1": "pay"})
        self.assertTrue(text.startswith("Billing is owned by A."))
        self.assertIn("A -[OWNS]-> Billing", text)
        self.assertIn("who_knows", text)
        self.assertIn("billing retries are flaky", text)
        self.assertIn("#pay", text)
        self.assertIn("Read from: public channels, #pay", text)

    def test_connected_tools_are_named_only_while_a_connector_is_configured(self):
        tags = ["org_shared", "org_internal", "slack_channel_G1"]
        with mock.patch.object(registry, "installs", return_value=[]):
            self.assertIn("Read from: public channels, #pay_", format_answer({"answer": "x", "path": []}, [], tags, {"G1": "pay"}))
            self.assertNotIn("connected tools", format_scopes(tags, "org_shared", {"G1": "pay"}))
        with mock.patch.object(registry, "installs", return_value=[(object(), object())]):
            self.assertIn("Read from: public channels, connected tools, #pay_", format_answer({"answer": "x", "path": []}, [], tags, {"G1": "pay"}))
            self.assertIn("• connected tools", format_scopes(tags, "org_shared", {"G1": "pay"}))

    def test_an_orgs_own_connectors_count_and_cited_tools_are_named(self):
        from src.core import tenancy
        from src.domain.knowledge.dto import PathStep, ToolResult

        tags = ["org_shared", "org_internal"]
        org = tenancy.OrgRef(id="o1", graph="org_" + "1" * 26)
        result = ToolResult(tool="who_owns", answer="x")
        result.add(PathStep("Dev Patel (HubSpot)", "OWNS", "Larkspur", from_id="hubspot:abc123:user:9", to_id="hubspot:abc123:company:1"))
        result.add(PathStep("Arjun (Jira)", "ASSIGNED", "PAY-1", from_id="jira:def456:user:7", to_id="jira:def456:10001"))
        result.add(PathStep("Billing", "DEPENDS_ON", "Postgres", from_id="billing-service", to_id="postgres"))
        self.assertEqual(result.to_dict()["sources"], ["hubspot", "jira"])
        with mock.patch.object(registry, "installs", return_value=[]), mock.patch.object(registry, "installs_of", return_value=[(object(), object())]):
            with tenancy.org_context(org):
                # An org's console install counts: the env installs (registry.installs) are the legacy org's only.
                self.assertIn("Read from: public channels, connected tools_", format_answer({"answer": "x", "path": []}, [], tags, {}))
                self.assertIn("Read from: public channels, HubSpot, Jira_", format_answer(result.to_dict(), [], tags, {}))
        self.assertNotIn("sources", ToolResult(tool="t", answer="x").to_dict())

    def test_public_memory_cites_channel(self):
        memories = [{"text": "hello there all", "scope": "org_shared", "channel_id": "C9", "author": "Bo"}]
        text = format_answer({"answer": "x", "path": []}, memories, ["org_shared"], {})
        self.assertIn("<#C9>", text)

    def test_path_header_names_tool_depth_and_edge_count(self):
        path = [{"from": f"n{i}", "rel": "DEPENDS_ON", "to": "auth"} for i in range(7)]
        text = format_answer({"answer": "x", "path": path, "tool": "blast_radius", "depth": 2}, [], ["org_shared"], {})
        self.assertIn("*Path* (`blast_radius`, depth 2, 7 edges)", text)
        one = format_answer({"answer": "x", "path": path[:1], "tool": "who_owns"}, [], ["org_shared"], {})
        self.assertIn("*Path* (`who_owns`, 1 edge)", one)

    def test_approved_write_footer_says_so(self):
        text = format_answer({"answer": "Filed", "path": [], "tool": "ingest_ticket"}, [], ["org_shared"], {}, chosen_by="approval")
        self.assertIn("Tool `ingest_ticket`, run after your approval. Read from: public channels", text)

    def test_answer_fallback_and_long_path(self):
        path = [{"from": f"n{i}", "rel": "DEPENDS_ON", "to": "auth"} for i in range(25)]
        text = format_answer({"answer": "", "path": path}, [], ["org_shared"], {})
        self.assertIn("couldn't find", text)
        self.assertIn("5 more edges", text)

    def test_scopes(self):
        text = format_scopes(["org_shared", "user_U1"], "user_U1", {})
        self.assertIn("public channels", text)
        self.assertIn("go to:* your DMs", text)
        self.assertIn("Nothing said here is saved", format_scopes(["org_shared"], None, {}))


if __name__ == "__main__":
    unittest.main()
