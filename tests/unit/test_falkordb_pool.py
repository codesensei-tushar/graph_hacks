"""The FalkorDB connection is bounded: connect, reply and pool waits all time out, an idle
connection is checked before reuse, and no command is ever sent twice."""

import unittest
from unittest import mock

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from src.db import falkordb_client as fc


class PoolSettingsTest(unittest.TestCase):
    def test_pool_is_bounded_and_blocking(self):
        pool = fc.pool()
        kw = pool.connection_kwargs
        self.assertEqual(type(pool).__name__, "BlockingConnectionPool")
        self.assertEqual(pool.max_connections, 64)
        self.assertEqual(pool.timeout, 5)
        self.assertEqual(kw["socket_connect_timeout"], 2.0)
        self.assertEqual(kw["health_check_interval"], 30)
        self.assertTrue(kw["decode_responses"])
        self.assertIsInstance(kw["retry"], fc.CommandsOnce)

    def test_socket_timeout_is_a_few_seconds_past_the_server_timeout(self):
        from tests.unit.test_falkordb_limits import COMPOSE, falkordb_args

        server_s = falkordb_args(COMPOSE[0])["TIMEOUT"] / 1000
        self.assertGreater(fc.SOCKET_TIMEOUT, server_s)
        self.assertLessEqual(fc.SOCKET_TIMEOUT, server_s + 10)


class CommandsOnceTest(unittest.TestCase):
    def test_a_command_is_never_retried(self):
        for error in (RedisConnectionError("gone"), RedisTimeoutError("slow")):
            with self.subTest(error=type(error).__name__):
                calls = []

                def send():
                    calls.append(1)
                    raise error

                with self.assertRaises(type(error)):
                    fc.CommandsOnce().call_with_retry(send, lambda *a: None, with_failure_count=True)
                self.assertEqual(len(calls), 1)

    def test_the_health_check_ping_gets_one_more_try(self):
        calls = []

        def _send_ping():
            calls.append(1)
            if len(calls) == 1:
                raise RedisConnectionError("stale")

        fc.CommandsOnce().call_with_retry(_send_ping, lambda *a: None, with_failure_count=True)
        self.assertEqual(len(calls), 2)

    def test_a_graph_write_is_sent_once_when_the_reply_is_lost(self):
        """End to end through redis-py: the socket times out reading the reply; the write is not resent."""
        pool = fc.pool()
        db = fc.FalkorDB(connection_pool=pool)
        g = db.select_graph("pool_test_unused")
        sent = []
        conn_cls = pool.connection_class

        def fake_send(self, *args, **kwargs):
            sent.append(args[0])

        def fake_read(self, *args, **kwargs):
            raise RedisTimeoutError("Timeout reading from socket")

        with (
            mock.patch.object(conn_cls, "send_command", fake_send),
            mock.patch.object(conn_cls, "read_response", fake_read),
            mock.patch.object(conn_cls, "connect", lambda self: None),
            mock.patch.object(conn_cls, "check_health", lambda self: None),
        ):
            pool.connection_kwargs["health_check_interval"] = 0
            with self.assertRaises(RedisTimeoutError):
                g.query("CREATE (:X)")
        self.assertEqual(sent, ["GRAPH.QUERY"])


if __name__ == "__main__":
    unittest.main()
