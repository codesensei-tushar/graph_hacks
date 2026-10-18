"""Against the dev FalkorDB: a pooled connection the server dropped while it sat idle is
replaced by the health check before the next command, which then succeeds. Without the PING's
second try (``CommandsOnce``) the command would fail with the stale connection's error."""

import unittest
from unittest import mock

from redis import Redis

from src.db import falkordb_client as fc


class StaleConnectionTest(unittest.TestCase):
    def test_a_dropped_idle_connection_is_replaced(self):
        pool = fc.pool()
        r = Redis(connection_pool=pool)
        conn = pool.get_connection()
        conn.send_command("CLIENT", "ID")
        stale_id = conn.read_response()
        pool.release(conn)
        Redis(connection_pool=fc.pool()).execute_command("CLIENT", "KILL", "ID", stale_id)
        conn.next_health_check = 0  # as if it had sat idle past HEALTH_CHECK_INTERVAL
        # A silent drop (a NAT or load balancer forgot the flow): checkout cannot see it, only the PING can.
        with mock.patch.object(type(conn), "can_read", lambda self, timeout=0: False):
            self.assertTrue(r.ping())
        self.assertNotEqual(r.execute_command("CLIENT", "ID"), stale_id)
        pool.disconnect()


if __name__ == "__main__":
    unittest.main()
