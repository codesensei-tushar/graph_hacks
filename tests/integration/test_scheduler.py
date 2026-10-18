"""The scheduler on the real Redis: atomic claims with leases, the worker pool, groups, the fast lane.

Each test uses a queue of its own (``scheduler.QUEUE`` and friends patched), so no other test's
jobs are in it and none of these leak out.
"""

import threading
import time
import unittest
import uuid
from unittest import mock

import tests  # noqa: F401  pins the test graph and the queue before src loads

from src.core import scheduler, tenancy  # noqa: E402
from src.db.falkordb_client import kv  # noqa: E402


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        queue = f"brain:schedule:test:sched:{uuid.uuid4().hex[:8]}"
        values = {"QUEUE": queue, "PAYLOADS": f"{queue}:payload", "RUNNING": f"{queue}:running", "INDEXED": f"{queue}:indexed"}
        values.update(_QUEUE=values["QUEUE"], _PAYLOADS=values["PAYLOADS"])
        for name, value in values.items():
            patch = mock.patch.object(scheduler, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self._clean, queue)
        self.ran: list = []
        self.names = [f"testJob{uuid.uuid4().hex[:6]}{n}" for n in range(3)]
        for name in self.names:
            scheduler.register(name, lambda payload, name=name: self.ran.append((name, payload.get("n"), tenancy.current_org())))
            self.addCleanup(scheduler._handlers.pop, name, None)

    def _clean(self, queue):
        keys = list(kv().scan_iter(match=f"{queue}*"))
        if keys:
            kv().delete(*keys)

    # --- claims ---------------------------------------------------------------------------------

    def test_a_due_job_is_claimed_by_exactly_one_of_many_claimers(self):
        scheduler.schedule(0, self.names[0], {"n": 1}, job_id="once")
        claims, barrier = [], threading.Barrier(8)

        def claim():
            barrier.wait()
            claims.append(scheduler._claim(time.time() + 1))

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        won = [claim for claim in claims if claim]
        self.assertEqual(len(won), 1)
        self.assertEqual(won[0][0], "once")
        # Claimed: out of the queue, in running with a lease, and its payload kept until it finishes.
        self.assertIsNone(kv().zscore(scheduler.QUEUE, "once"))
        self.assertGreater(kv().zscore(scheduler.RUNNING, "once"), time.time() + scheduler.LEASE_SECONDS - 5)
        self.assertIsNotNone(kv().hget(scheduler.PAYLOADS, "once"))
        scheduler._run_claimed(*won[0])
        self.assertEqual(self.ran, [(self.names[0], 1, tenancy.LEGACY_ORG_ID)])
        self.assertIsNone(kv().hget(scheduler.PAYLOADS, "once"))
        self.assertIsNone(kv().zscore(scheduler.RUNNING, "once"))

    def test_a_handler_that_re_arms_its_own_job_keeps_the_new_one(self):
        def step(payload):
            self.ran.append(payload["n"])
            if payload["n"] < 3:
                scheduler.schedule(0, "testStep", {"n": payload["n"] + 1}, job_id="step")

        scheduler.register("testStep", step)
        self.addCleanup(scheduler._handlers.pop, "testStep", None)
        scheduler.schedule(0, "testStep", {"n": 1}, job_id="step")
        for _ in range(5):
            scheduler.run_due(time.time() + 1)
        self.assertEqual(self.ran, [1, 2, 3])
        self.assertIsNone(kv().hget(scheduler.PAYLOADS, "step"))

    def test_a_job_whose_worker_died_runs_again_after_its_lease(self):
        scheduler.schedule(0, self.names[0], {"n": 7}, job_id="orphan")
        claimed = scheduler._claim(time.time() + 1)  # a worker claims it, then its process is killed
        self.assertEqual(claimed[0], "orphan")
        self.assertEqual(scheduler.run_due(time.time() + 1), 0)  # leased: nobody else runs it
        later = time.time() + scheduler.LEASE_SECONDS + 1
        kv().delete(claimed[2])  # the lock's own expiry, which Redis would have applied by then
        self.assertEqual(scheduler.reap(later), 1)
        self.assertEqual(scheduler.run_due(later), 1)
        self.assertEqual(self.ran, [(self.names[0], 7, tenancy.LEGACY_ORG_ID)])

    def test_a_running_job_keeps_its_lease(self):
        scheduler.schedule(0, self.names[0], {}, job_id="long")
        stored, _raw, lock = scheduler._claim(time.time() + 1)
        with scheduler._running_lock:
            scheduler._running[stored] = lock
        try:
            kv().zadd(scheduler.RUNNING, {stored: time.time() + 1})
            kv().pexpire(lock, 1000)
            scheduler._renew()
            self.assertGreater(kv().zscore(scheduler.RUNNING, stored), time.time() + scheduler.LEASE_SECONDS - 5)
            self.assertGreater(kv().pttl(lock), (scheduler.LEASE_SECONDS - 5) * 1000)
        finally:
            with scheduler._running_lock:
                scheduler._running.pop(stored, None)

    # --- one at a time per (org, group) --------------------------------------------------------

    def test_one_job_of_a_group_runs_at_a_time_per_org(self):
        org_b = tenancy.OrgRef(id="orgb", graph="org_" + "b" * 26)
        resolve = tenancy.resolve
        with mock.patch.object(tenancy, "resolve", side_effect=lambda org: org_b if org == "orgb" else resolve(org)):
            scheduler.schedule(0, self.names[0], {"n": 1}, job_id="a1")
            scheduler.schedule(0, self.names[0], {"n": 2}, job_id="a2")
            scheduler.schedule(0, self.names[1], {"n": 3}, job_id="other")
            with tenancy.org_context(org_b):
                scheduler.schedule(0, self.names[0], {"n": 4}, job_id="b1")
            now = time.time() + 1
            first = scheduler._claim(now)
            held = [first]
            while (claimed := scheduler._claim(now)) is not None:
                held.append(claimed)
            # One of a1/a2 (same name, same org), plus the other name, plus org B's: never a1 and a2 together.
            ids = sorted(claim[0] for claim in held)
            self.assertEqual(len(ids), 3, ids)
            self.assertEqual(len({"a1", "a2"} & set(ids)), 1)
            self.assertIn("other", ids)
            self.assertIn("orgb:b1", ids)
            for claim in held:
                scheduler._run_claimed(*claim)
            # Released: the one left waiting runs now.
            self.assertEqual(scheduler.run_due(time.time() + 1), 1)
        self.assertEqual(sorted(n for _name, n, _org in self.ran), [1, 2, 3, 4])

    def test_connector_jobs_of_one_org_share_a_group(self):
        self.assertEqual(scheduler.GROUPS["runConnectorSync"], scheduler.GROUPS["runConnectorRemoval"])
        self.assertEqual(scheduler.GROUPS["runConnectorSync"], scheduler.GROUPS["runConnectorPurge"])

    # --- the fast lane and the pool --------------------------------------------------------------

    def test_the_fast_lane_takes_only_fast_jobs(self):
        with mock.patch.object(scheduler, "FAST", frozenset({self.names[2]})):
            scheduler.schedule(0, self.names[0], {"n": 1}, job_id="slow")
            scheduler.schedule(0, self.names[2], {"n": 2}, job_id="fast")
            claimed = scheduler._claim(time.time() + 1, "fast")
            self.assertEqual(claimed[0], "fast")
            scheduler._run_claimed(*claimed)
            self.assertIsNone(scheduler._claim(time.time() + 1, "fast"))
            self.assertIsNotNone(kv().zscore(scheduler.QUEUE, "slow"))

    def test_an_expiry_is_not_held_up_by_slow_jobs(self):
        """Every general worker is busy with a slow job; the approval expiry still runs within a poll."""
        release = threading.Event()
        expired = threading.Event()
        scheduler.register("testSlow", lambda payload: release.wait(10))
        scheduler.register("testExpiry", lambda payload: expired.set())
        self.addCleanup(scheduler._handlers.pop, "testSlow", None)
        self.addCleanup(scheduler._handlers.pop, "testExpiry", None)
        stop = threading.Event()
        with mock.patch.object(scheduler, "FAST", frozenset({"testExpiry"})), mock.patch.object(scheduler, "_POLL_SECONDS", 0.05):
            for n in range(4):
                with tenancy.org_context(tenancy.OrgRef(id=f"org{n}", graph="org_" + str(n) * 26)):
                    scheduler.schedule(0, "testSlow", {}, job_id=f"slow{n}")
            resolve = tenancy.resolve
            with mock.patch.object(tenancy, "resolve", side_effect=lambda org: tenancy.OrgRef(id=org, graph="org_" + "0" * 26) if isinstance(org, str) and org.startswith("org") else resolve(org)):
                workers = [threading.Thread(target=scheduler._worker, args=("any", stop), daemon=True) for _ in range(4)]
                workers.append(threading.Thread(target=scheduler._worker, args=("fast", stop), daemon=True))
                for worker in workers:
                    worker.start()
                time.sleep(0.3)  # every general worker now holds a slow job
                started = time.monotonic()
                scheduler.schedule(0, "testExpiry", {}, job_id="expiry")
                self.assertTrue(expired.wait(2), "the expiry waited behind the slow jobs")
                waited = time.monotonic() - started
                release.set()
                stop.set()
                for worker in workers:
                    worker.join(3)
        self.assertLess(waited, 1.0)

    # --- cancelling by name and by org, through the indexes ---------------------------------------

    def test_cancel_matching_and_cancel_org_read_the_indexes_not_every_job(self):
        scheduler._ensure_indexes()  # start() does this once; the queue is empty, so it only sets the marker
        for n in range(5):
            scheduler.schedule(60, self.names[0], {"n": n}, job_id=f"j{n}")
        scheduler.schedule(60, self.names[1], {"n": 9}, job_id="keep")
        with mock.patch.object(type(kv()), "hgetall", side_effect=AssertionError("cancel scanned every job")):
            self.assertEqual(scheduler.cancel_matching(self.names[0], lambda payload: payload["n"] % 2 == 0), 3)
            self.assertEqual(scheduler.cancel_matching(self.names[0]), 2)
            self.assertEqual(scheduler.cancel_org(tenancy.LEGACY_ORG_ID), 1)
        self.assertEqual(kv().zcard(scheduler.QUEUE), 0)
        self.assertEqual(kv().hlen(scheduler.PAYLOADS), 0)

    def test_jobs_queued_before_the_indexes_are_indexed_once(self):
        kv().hset(scheduler.PAYLOADS, "old", '{"name": "%s", "payload": {}}' % self.names[0])
        kv().zadd(scheduler.QUEUE, {"old": time.time() + 60})
        self.assertEqual(scheduler.cancel_matching(self.names[0]), 1)
        self.assertEqual(kv().get(scheduler.INDEXED), "1")


if __name__ == "__main__":
    unittest.main()
