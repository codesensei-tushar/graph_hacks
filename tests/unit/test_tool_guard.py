"""A graph query that runs out of time is a sentence, logged once; any other error passes through. No database."""

import dataclasses
import unittest

import tests  # noqa: F401  pins the test settings before src loads

from redis.exceptions import ConnectionError as RedisConnectionError  # noqa: E402
from redis.exceptions import ResponseError  # noqa: E402
from redis.exceptions import TimeoutError as RedisTimeoutError  # noqa: E402

from src.domain.knowledge import catalogue, mcp_tools, tool_guard  # noqa: E402


class GuardTest(unittest.TestCase):
    def test_falkordb_and_socket_timeouts_become_the_reply_and_are_logged(self):
        for error in (ResponseError("Query timed out"), RedisTimeoutError("Timeout reading from socket")):
            with self.assertLogs("src.domain.knowledge.tool_guard", "WARNING") as logged:
                with self.assertRaises(tool_guard.ToolTimedOut) as caught:
                    with tool_guard.guard("ask", "how is   maya connected to billing"):
                        raise error
            self.assertEqual(str(caught.exception), "That took too long; try a narrower question.")
            self.assertIs(caught.exception.__cause__, error)
            self.assertIn("[ask] graph query timed out", logged.output[0])
            self.assertIn("how is maya connected to billing", logged.output[0])

    def test_other_errors_pass_through_untouched(self):
        for error in (ResponseError("errMsg: Invalid input"), RedisConnectionError("refused"), ValueError("bad")):
            with self.assertRaises(type(error)) as caught:
                with tool_guard.guard("ask"):
                    raise error
            self.assertIs(caught.exception, error)

    def test_a_guard_inside_a_guard_logs_once(self):
        with self.assertLogs("src.domain.knowledge.tool_guard", "WARNING") as logged:
            with self.assertRaises(tool_guard.ToolTimedOut):
                with tool_guard.guard("outer"):
                    with tool_guard.guard("inner"):
                        raise ResponseError("Query timed out")
        self.assertEqual(len(logged.output), 1)


class McpTimeoutTest(unittest.TestCase):
    def test_a_tool_that_times_out_is_a_tool_error_with_the_reply(self):
        original = catalogue.get_tool("who_owns")

        def run(args, scopes):
            raise ResponseError("Query timed out")

        catalogue.register(dataclasses.replace(original, run=run))
        try:
            with self.assertLogs("src.domain.knowledge.tool_guard", "WARNING"):
                with self.assertRaisesRegex(mcp_tools.ToolError, "That took too long; try a narrower question."):
                    mcp_tools.call_tool("who_owns", {"name": "billing"})
        finally:
            catalogue.register(original)


if __name__ == "__main__":
    unittest.main()
