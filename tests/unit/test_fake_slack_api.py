"""The fake Slack API's control endpoints and clock, which a loader in another process relies on."""

import json
import time
import unittest
import urllib.request

from scripts.replay.slack_api import FakeSlackChannel, FakeSlackServer, FakeSlackUser, FakeSlackWorkspace


class FakeSlackControlTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSlackWorkspace(team_id="T1", team_name="Test", bot_user_id="UBOT", bot_token="replay-token")
        self.fake.add_user(FakeSlackUser(id="UANN", name="ann", real_name="Ann"))
        self.fake.add_channel(FakeSlackChannel(id="CGEN", name="general", members=["UANN", "UBOT"]))
        self.server = FakeSlackServer(self.fake).start()

    def tearDown(self):
        self.server.stop()

    def _json(self, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.port}{path}", data=data, method="POST" if data else "GET", headers={"content-type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def test_say_then_messages_round_trip_with_blocks(self):
        said = self._json("/replay/say", {"channel": "CGEN", "user": "UANN", "text": "hello there", "ts": "1700000000.000100"})
        self.assertEqual(said, {"ok": True, "ts": "1700000000.000100"})
        card = {"channel": "CGEN", "text": "hi", "thread_ts": "1700000000.000100", "blocks": [{"type": "actions", "elements": [{"action_id": "x", "value": "approval-1"}]}]}
        self.assertTrue(self._json("/api/chat.postMessage?token=replay-token", card)["ok"])
        thread = self._json("/replay/messages?channel=CGEN&thread_ts=1700000000.000100")["messages"]
        self.assertEqual([m["user"] for m in thread], ["UANN", "UBOT"])
        self.assertEqual(thread[1]["blocks"][0]["elements"][0]["value"], "approval-1")
        self.assertFalse(thread[1]["streaming"])

    def test_clock_is_wall_clock_and_strictly_increasing(self):
        first = self.fake.next_ts()
        self.assertGreater(float(first), time.time() - 5)
        self.assertGreater(self.fake.next_ts(), first)
        # A message stamped a little later by another process still sorts before the next reply.
        later = f"{time.time() + 1:.6f}"
        self.fake.say("CGEN", "UANN", "from a loader", ts=later)
        self.assertGreater(self.fake.next_ts(), later)


if __name__ == "__main__":
    unittest.main()
