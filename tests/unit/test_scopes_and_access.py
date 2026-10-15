import json
import unittest
from urllib.parse import urlencode

from src.api.v1.slack import parse_interaction_payload
from src.domain.access.roles import role_at_least
from src.domain.memory.writeback import (
    SHARED_TEAM_BRAIN_CONTAINER_TAG,
    SlackMemoryScope,
    scope_label,
    slack_memory_container_tag,
    slack_memory_scope_for_turn,
)
from src.integrations.slack.client import SlackConversationInfo


class ContainerTagTests(unittest.TestCase):
    def test_tags_by_kind(self):
        self.assertEqual(slack_memory_container_tag(SlackMemoryScope(kind="shared", channel_id="C1")), "org_shared")
        self.assertEqual(slack_memory_container_tag(SlackMemoryScope(kind="private_channel", channel_id="G1")), "slack_channel_G1")
        self.assertEqual(slack_memory_container_tag(SlackMemoryScope(kind="dm", channel_id="D1", user_id="U1")), "user_U1")
        self.assertIsNone(slack_memory_container_tag(SlackMemoryScope(kind="dm", channel_id="D1")))
        self.assertEqual(slack_memory_container_tag(None), SHARED_TEAM_BRAIN_CONTAINER_TAG)


class ScopeForTurnTests(unittest.TestCase):
    def test_dm(self):
        scope = slack_memory_scope_for_turn(is_dm=True, channel="D1", channel_type="im", user_id="U1", slack_user_id="U1")
        self.assertEqual((scope.kind, scope.user_id, scope.slack_user_id), ("dm", "U1", "U1"))

    def test_public_channel(self):
        scope = slack_memory_scope_for_turn(is_dm=False, channel="C1", channel_type="channel", user_id="U1")
        self.assertEqual(scope.kind, "shared")
        self.assertIsNone(scope.user_id)

    def test_group_is_private(self):
        scope = slack_memory_scope_for_turn(is_dm=False, channel="C1", channel_type="group")
        self.assertEqual(scope.kind, "private_channel")

    def test_unknown_type_fails_closed(self):
        scope = slack_memory_scope_for_turn(is_dm=False, channel="C1")
        self.assertEqual(scope.kind, "private_channel")

    def test_conversation_info_upgrades_to_private(self):
        info = SlackConversationInfo(id="C1", is_private=True)
        scope = slack_memory_scope_for_turn(is_dm=False, channel="C1", channel_type="channel", conversation_info=info)
        self.assertEqual(scope.kind, "private_channel")

    def test_public_info_does_not_downgrade(self):
        info = SlackConversationInfo(id="G1", is_private=False)
        scope = slack_memory_scope_for_turn(is_dm=False, channel="G1", channel_type="group", conversation_info=info)
        self.assertEqual(scope.kind, "private_channel")


class ScopeLabelTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(scope_label("org_shared"), "public channels")
        self.assertEqual(scope_label("slack_channel_G1", {"G1": "deals"}), "#deals")
        self.assertEqual(scope_label("slack_channel_G2"), "<#G2>")
        self.assertEqual(scope_label("user_U1"), "your DMs")
        self.assertEqual(scope_label("other"), "other")


class RoleTests(unittest.TestCase):
    def test_ranks(self):
        self.assertTrue(role_at_least("owner", "admin"))
        self.assertTrue(role_at_least("admin", "admin"))
        self.assertFalse(role_at_least("member", "admin"))
        self.assertTrue(role_at_least("member", "member"))
        self.assertFalse(role_at_least("admin", "owner"))
        self.assertFalse(role_at_least(None, "member"))
        self.assertFalse(role_at_least("stranger", "member"))


def _payload(action_id: str, **overrides) -> bytes:
    payload = {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "response_url": "https://hooks.example.com/r",
        "actions": [{"action_id": action_id, "value": "ap1"}],
    }
    payload.update(overrides)
    return urlencode({"payload": json.dumps(payload)}).encode()


class InteractionPayloadTests(unittest.TestCase):
    def test_approve(self):
        parsed = parse_interaction_payload(_payload("brain_approval_approve"))
        self.assertEqual(
            parsed,
            {"team_id": "T1", "user_id": "U1", "approval_id": "ap1", "approved": True, "response_url": "https://hooks.example.com/r"},
        )

    def test_deny(self):
        self.assertFalse(parse_interaction_payload(_payload("brain_approval_deny"))["approved"])

    def test_wrong_action_id(self):
        self.assertIsNone(parse_interaction_payload(_payload("something_else")))

    def test_wrong_type(self):
        self.assertIsNone(parse_interaction_payload(_payload("brain_approval_approve", type="view_submission")))

    def test_malformed(self):
        self.assertIsNone(parse_interaction_payload(b""))
        self.assertIsNone(parse_interaction_payload(b"payload=not-json"))
        self.assertIsNone(parse_interaction_payload(_payload("brain_approval_approve", team={})))


if __name__ == "__main__":
    unittest.main()
