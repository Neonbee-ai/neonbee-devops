import copy
import os
import unittest

from _load import load

cf = load("cf_lb_sync")

DETAIL = {
    "EC2InstanceId": "i-new",
    "LifecycleHookName": "nb-ready",
    "AutoScalingGroupName": "nb-prod-asg",
    "LifecycleActionToken": "tok",
}
FALLBACK = {"name": "dev-vm", "address": "84.247.131.148", "enabled": True, "weight": 1}


class FakeDeps(object):
    def __init__(self, origins=None, ip="13.0.0.5", healthy_after=0, cf_fail=None, live=None):
        self.origins = copy.deepcopy(origins if origins is not None else [FALLBACK])
        self.ip = ip
        self.healthy_after = healthy_after   # health checks that fail before success; -1 = never
        self.cf_fail = cf_fail               # None | "get" | "put"
        self.live = live or {}
        self.clock = 0
        self.health_calls = 0
        self.puts = []
        self.alerts = []
        self.completed = []
        self.sleeps = []

    def public_ip(self, iid):
        return self.ip

    def node_healthy(self, iid):
        self.health_calls += 1
        self.clock += 5
        return self.healthy_after >= 0 and self.health_calls > self.healthy_after

    def complete(self, detail, result):
        self.completed.append(result)

    def in_service(self, asg):
        return self.live

    def alert(self, title, text):
        self.alerts.append(title)

    def sleep(self, s):
        self.sleeps.append(s)
        self.clock += s

    def now(self):
        return self.clock

    def get_origins(self):
        if self.cf_fail == "get":
            raise cf.CloudflareError("HTTP 403")
        return copy.deepcopy(self.origins)

    def put_origins(self, origins):
        if self.cf_fail == "put":
            raise cf.CloudflareError("HTTP 500")
        self.puts.append(copy.deepcopy(origins))
        self.origins = copy.deepcopy(origins)


def launch_event():
    return {"detail-type": cf.LAUNCH, "detail": dict(DETAIL)}


def terminate_event(iid="i-old"):
    d = dict(DETAIL, EC2InstanceId=iid, LifecycleHookName="nb-drain")
    return {"detail-type": cf.TERMINATE, "detail": d}


class CfLbSyncTest(unittest.TestCase):
    def setUp(self):
        os.environ.update({"ORIGIN_PREFIX": "aws-", "HEALTH_WAIT_SECONDS": "60",
                           "HEALTH_POLL_SECONDS": "10", "DRAIN_SECONDS": "45",
                           "ORIGIN_WEIGHT": "1", "ASG_NAME": "nb-prod-asg"})

    # ---------------------------------------------------------------- launch
    def test_given_healthy_new_node_when_launch_hook_fires_then_origin_added_and_continue(self):
        deps = FakeDeps(healthy_after=2)
        self.assertEqual(cf.handler(launch_event(), None, deps), "added")
        names = [o["name"] for o in deps.origins]
        self.assertEqual(names, ["dev-vm", "aws-i-new"])
        self.assertEqual(deps.origins[1]["address"], "13.0.0.5")
        self.assertTrue(deps.origins[1]["enabled"])
        self.assertEqual(deps.completed, ["CONTINUE"])
        self.assertEqual(deps.alerts, [])

    def test_given_node_never_healthy_when_launch_hook_fires_then_abandon_and_alert_and_pool_untouched(self):
        deps = FakeDeps(healthy_after=-1)
        self.assertEqual(cf.handler(launch_event(), None, deps), "abandoned:unhealthy")
        self.assertEqual(deps.completed, ["ABANDON"])
        self.assertEqual(deps.puts, [])
        self.assertEqual(len(deps.alerts), 1)

    def test_given_node_without_public_ip_when_launch_hook_fires_then_abandon_without_health_wait(self):
        deps = FakeDeps(ip=None)
        self.assertEqual(cf.handler(launch_event(), None, deps), "abandoned:no-ip")
        self.assertEqual(deps.health_calls, 0)
        self.assertEqual(deps.completed, ["ABANDON"])

    def test_given_cloudflare_rejects_update_when_launch_hook_fires_then_abandon_and_alert_teams(self):
        deps = FakeDeps(cf_fail="put")
        self.assertEqual(cf.handler(launch_event(), None, deps), "abandoned:cloudflare")
        self.assertEqual(deps.completed, ["ABANDON"])
        self.assertIn("Cloudflare pool add failed", deps.alerts[0])

    def test_given_stale_origin_with_same_ip_when_launch_hook_fires_then_it_is_replaced_not_duplicated(self):
        stale = {"name": "aws-i-dead", "address": "13.0.0.5", "enabled": True, "weight": 1}
        deps = FakeDeps(origins=[FALLBACK, stale])
        cf.handler(launch_event(), None, deps)
        self.assertEqual([o["name"] for o in deps.origins], ["dev-vm", "aws-i-new"])

    # ---------------------------------------------------------------- terminate
    def test_given_node_in_pool_when_terminate_hook_fires_then_disabled_drained_removed_continue(self):
        old = {"name": "aws-i-old", "address": "13.0.0.9", "enabled": True, "weight": 1}
        deps = FakeDeps(origins=[FALLBACK, old])
        self.assertEqual(cf.handler(terminate_event(), None, deps), "removed")
        self.assertFalse(deps.puts[0][1]["enabled"])          # first write = drain
        self.assertEqual(deps.sleeps, [45])
        self.assertEqual([o["name"] for o in deps.origins], ["dev-vm"])
        self.assertEqual(deps.completed, ["CONTINUE"])

    def test_given_node_is_last_origin_when_terminate_hook_fires_then_disabled_not_removed_and_alert(self):
        only = {"name": "aws-i-old", "address": "13.0.0.9", "enabled": True, "weight": 1}
        deps = FakeDeps(origins=[only])
        self.assertEqual(cf.handler(terminate_event(), None, deps), "disabled-last-origin")
        self.assertEqual(len(deps.origins), 1)
        self.assertFalse(deps.origins[0]["enabled"])
        self.assertEqual(len(deps.alerts), 1)

    def test_given_cloudflare_down_when_terminate_hook_fires_then_alert_and_still_continue(self):
        deps = FakeDeps(cf_fail="get")
        self.assertEqual(cf.handler(terminate_event(), None, deps), "cloudflare-failed")
        self.assertEqual(deps.completed, ["CONTINUE"])
        self.assertIn("Cloudflare pool remove failed", deps.alerts[0])

    def test_given_node_not_in_pool_when_terminate_hook_fires_then_no_write_and_continue(self):
        deps = FakeDeps()
        self.assertEqual(cf.handler(terminate_event(), None, deps), "not-in-pool")
        self.assertEqual(deps.puts, [])
        self.assertEqual(deps.completed, ["CONTINUE"])

    # ---------------------------------------------------------------- reconcile
    def test_given_orphan_aws_origin_when_reconcile_runs_then_removed_and_fallback_kept(self):
        orphan = {"name": "aws-i-gone", "address": "1.1.1.1", "enabled": True, "weight": 1}
        live = {"name": "aws-i-live", "address": "2.2.2.2", "enabled": True, "weight": 1}
        deps = FakeDeps(origins=[FALLBACK, orphan, live], live={"i-live": "2.2.2.2"})
        out = cf.handler({"detail-type": cf.SCHEDULED}, None, deps)
        self.assertTrue(out["changed"])
        self.assertEqual([o["name"] for o in deps.origins], ["dev-vm", "aws-i-live"])
        self.assertEqual(deps.alerts, [])

    def test_given_inservice_node_missing_from_pool_when_reconcile_runs_then_alert_without_write(self):
        deps = FakeDeps(live={"i-lost": "3.3.3.3"})
        out = cf.handler({"detail-type": cf.SCHEDULED}, None, deps)
        self.assertEqual(out["missing"], ["i-lost"])
        self.assertEqual(deps.puts, [])
        self.assertEqual(len(deps.alerts), 1)

    def test_given_cloudflare_down_when_reconcile_runs_then_alert_and_raise(self):
        deps = FakeDeps(cf_fail="get")
        with self.assertRaises(cf.CloudflareError):
            cf.handler({"detail-type": cf.SCHEDULED}, None, deps)
        self.assertEqual(len(deps.alerts), 1)

    def test_given_unrelated_event_when_handled_then_ignored(self):
        self.assertEqual(cf.handler({"detail-type": "Other"}, None, FakeDeps()), "ignored")


if __name__ == "__main__":
    unittest.main()
