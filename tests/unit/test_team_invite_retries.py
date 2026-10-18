"""team_join / user_change retries end: Retry-After sets the delay, but every try counts."""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test settings before src loads

from src.domain.slack import team_invite  # noqa: E402


class LifecycleRetryTest(unittest.TestCase):
    def _run_until_quiet(self, handler, process_name: str, outcome) -> list[tuple[float, dict]]:
        scheduled: list[tuple[float, dict]] = []
        payload = {"team_id": "T1", "user": {"id": "U1"}, "attempt": 1}
        with mock.patch.object(team_invite, process_name, side_effect=outcome), mock.patch.object(
            team_invite.scheduler, "schedule", side_effect=lambda delay, name, body, **_: scheduled.append((delay, body))
        ):
            for _ in range(100):
                before = len(scheduled)
                handler(payload)
                if len(scheduled) == before:
                    break
                payload = scheduled[-1][1]
        return scheduled

    def test_a_slack_outage_answering_retry_after_does_not_retry_forever(self):
        for handler, process in (
            (team_invite.run_slack_user_change_retry, "process_slack_user_change"),
            (team_invite.run_slack_team_join_retry, "process_slack_team_join"),
        ):
            with self.subTest(handler=handler.__name__):
                scheduled = self._run_until_quiet(handler, process, lambda *a: {"terminal": False, "retry_after_seconds": 30})
                self.assertEqual(len(scheduled), team_invite.MAX_LIFECYCLE_TRIES - 1)
                self.assertTrue(all(delay == 30 for delay, _ in scheduled))  # Retry-After is honoured as the delay

    def test_failures_without_retry_after_stop_at_the_attempt_limit(self):
        def boom(*args):
            raise RuntimeError("slack down")

        scheduled = self._run_until_quiet(team_invite.run_slack_user_change_retry, "process_slack_user_change", boom)
        self.assertEqual(len(scheduled), team_invite.MAX_TARGET_ATTEMPTS - 1)


if __name__ == "__main__":
    unittest.main()
