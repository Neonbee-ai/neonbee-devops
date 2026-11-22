"""BDD: Cloudflare HA -> AWS cut-over script (infra/aws/cloudflare/cf_cutover.py)."""
import importlib.util
import os
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cf_cutover", ROOT / "infra/aws/cloudflare/cf_cutover.py")
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)
WF = (ROOT / ".github/workflows/neonbee-cf-cutover.yml").read_text()

EIPS = ("3.110.17.10", "13.233.75.233")
HA = cc.HA_IP


def rec(i, content, name="help.neonbee.app", typ="A", proxied=True, comment=None):
    return {"id": i, "name": name, "type": typ, "content": content, "proxied": proxied, "ttl": 1,
            "comment": comment}


class FakeCF:
    """In-memory Cloudflare: one zone, records list, call log."""

    def __init__(self, records, zones=("neonbee.app",), label="CF_SAAS_API_TOKEN"):
        self.records, self.calls, self.label, self.n = list(records), [], label, 100
        self.zones = [{"id": "z-" + z, "name": z, "status": "active"} for z in zones]

    def list(self, path, params=None):
        if path == "/zones":
            return self.zones
        if path.endswith("/dns_records"):
            name = (params or {}).get("name")
            return [dict(r) for r in self.records if not name or r["name"] == name]
        if path == "/certificates":
            return []
        raise AssertionError(path)

    def call(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/settings/ssl"):
            return {"result": {"value": "strict"}}
        if method == "POST" and path.endswith("/dns_records"):
            self.n += 1
            self.records.append(dict(body, id=f"new{self.n}"))
        elif method == "PATCH":
            rid = path.rsplit("/", 1)[1]
            next(r for r in self.records if r["id"] == rid).update(body)
        elif method == "DELETE":
            rid = path.rsplit("/", 1)[1]
            self.records = [r for r in self.records if r["id"] != rid]
        elif method == "POST" and path == "/certificates":
            return {"result": {"id": "c1", "hostnames": body["hostnames"], "certificate": "-----BEGIN CERT"}}
        return {"result": {}}


ALLOWED = {"help.neonbee.app", "api.neonbee.app", "*.neonbee.store", "tools.neonbee.app"}


class HostGuardTest(unittest.TestCase):
    def test_given_bundle_sites_when_reading_then_prod_hosts_are_listed(self):
        hosts = cc.bundle_hosts()
        for h in ("api.neonbee.app", "dashboard.neonbee.app", "sso.neonbee.app", "*.neonbee.store"):
            self.assertIn(h, hosts)
        self.assertNotIn("_", hosts)

    def test_given_dev_or_preview_host_when_checked_then_it_is_refused(self):
        for h in ("dev-dashboard.neonbee.app", "pr-fix-x.skyoffice360.com", "tools.neonbee.app",
                  "autoqa.moonhive.in", "dev.api.neonbee.app"):
            with self.assertRaises(cc.CutoverError):
                cc.check_host(h, ALLOWED | {h})

    def test_given_host_outside_aws_bundle_when_checked_then_it_is_refused(self):
        with self.assertRaises(cc.CutoverError):
            cc.check_host("neonbee.store", ALLOWED)


class SwitchTest(unittest.TestCase):
    def test_given_ha_record_when_switching_then_second_eip_is_added_before_ha_content_flips(self):
        cf = FakeCF([rec("r1", HA)])
        res = cc.dns_change([cf], "help.neonbee.app", EIPS, ALLOWED, revert=False, dry_run=False)
        self.assertTrue(res["verified"])
        writes = [(m, p) for m, p, _ in cf.calls]
        self.assertEqual(writes[0][0], "POST")
        self.assertEqual(writes[1], ("PATCH", "/zones/z-neonbee.app/dns_records/r1"))
        self.assertEqual(sorted(r["content"] for r in cf.records), sorted(EIPS))
        self.assertTrue(all(r["proxied"] for r in cf.records))
        self.assertTrue(next(r for r in cf.records if r["id"] == "r1")["comment"].startswith(cc.WAS + HA))
        self.assertEqual(res["before"][0]["content"], HA)

    def test_given_dry_run_when_switching_then_nothing_is_written(self):
        cf = FakeCF([rec("r1", HA)])
        res = cc.dns_change([cf], "help.neonbee.app", EIPS, ALLOWED, revert=False, dry_run=True)
        self.assertEqual(cf.calls, [])
        self.assertEqual(len(res["plan"]), 2)

    def test_given_already_switched_when_switching_again_then_it_is_a_no_op(self):
        cf = FakeCF([rec("r1", EIPS[0]), rec("r2", EIPS[1])])
        res = cc.dns_change([cf], "help.neonbee.app", EIPS, ALLOWED, revert=False, dry_run=False)
        self.assertEqual(res["plan"], [])
        self.assertEqual(cf.calls, [])

    def test_given_unexpected_records_when_switching_then_it_refuses(self):
        cases = [[rec("r1", "1.2.3.4")], [rec("r1", HA, proxied=False)], [rec("r1", "x.example", typ="CNAME")],
                 [rec("r1", HA), rec("r2", "2a01::1", typ="AAAA")], [],
                 [rec("r1", HA), rec("r2", EIPS[0]), rec("r3", EIPS[1])]]
        for records in cases:
            with self.assertRaises(cc.CutoverError, msg=records):
                cc.plan_switch(records, EIPS)

    def test_given_wildcard_store_host_when_switching_then_zone_is_resolved(self):
        cf = FakeCF([rec("r1", HA, name="*.neonbee.store")], zones=("neonbee.store",))
        res = cc.dns_change([cf], "*.neonbee.store", EIPS, ALLOWED, revert=False, dry_run=True)
        self.assertEqual(res["zone"], "neonbee.store")


class RevertTest(unittest.TestCase):
    def test_given_switched_host_when_reverting_then_ha_is_restored_and_only_added_record_removed(self):
        cf = FakeCF([rec("r1", HA)])
        cc.dns_change([cf], "help.neonbee.app", EIPS, ALLOWED, revert=False, dry_run=False)
        cf.calls.clear()
        res = cc.dns_change([cf], "help.neonbee.app", EIPS, ALLOWED, revert=True, dry_run=False)
        self.assertTrue(res["verified"])
        self.assertEqual([(r["id"], r["content"]) for r in cf.records], [("r1", HA)])
        self.assertEqual(cf.calls[0][0], "PATCH")  # HA serves again before anything is removed
        self.assertEqual([m for m, _, _ in cf.calls].count("DELETE"), 1)

    def test_given_eip_record_not_made_by_tool_when_reverting_then_it_refuses_without_deleting(self):
        records = [rec("r1", EIPS[0], comment=cc.WAS + HA), rec("r9", EIPS[1], comment="hand made")]
        with self.assertRaises(cc.CutoverError):
            cc.plan_revert(records, EIPS)

    def test_given_non_added_record_when_applying_remove_then_it_is_never_deleted(self):
        cf = FakeCF([rec("r1", EIPS[0])])
        with self.assertRaises(cc.CutoverError):
            cc.apply(cf, "z", [("remove-added", rec("r1", EIPS[0]), None)])
        self.assertEqual(cf.calls, [])


class InventoryAndCertTest(unittest.TestCase):
    def test_given_zone_when_inventorying_then_only_ha_and_eip_records_are_listed_with_flags(self):
        cf = FakeCF([rec("r1", HA, name="api.neonbee.app"), rec("r2", HA, name="dev-dashboard.neonbee.app"),
                     rec("r3", "9.9.9.9", name="other.neonbee.app")], zones=("neonbee.app", "moonhive.in"))
        rep = cc.inventory([cf], EIPS, ALLOWED)
        self.assertEqual([z["zone"] for z in rep], ["neonbee.app"])
        self.assertEqual(rep[0]["ssl"], "strict")
        names = {r["name"]: r for r in rep[0]["records"]}
        self.assertEqual(set(names), {"api.neonbee.app", "dev-dashboard.neonbee.app"})
        self.assertIsNone(names["api.neonbee.app"]["excluded"])
        self.assertIsNotNone(names["dev-dashboard.neonbee.app"]["excluded"])

    def test_given_csr_when_signing_then_zone_and_wildcard_are_requested(self):
        cf = FakeCF([])
        res = cc.origin_cert([cf], "neonbee.app", "-----BEGIN CERTIFICATE REQUEST-----", dry_run=False)
        body = next(b for m, p, b in cf.calls if p == "/certificates")
        self.assertEqual(body["hostnames"], ["neonbee.app", "*.neonbee.app"])
        self.assertEqual(res["certificate"], "-----BEGIN CERT")

    def test_given_private_key_input_when_signing_then_it_refuses(self):
        with self.assertRaises(cc.CutoverError):
            cc.origin_cert([FakeCF([])], "neonbee.app", "-----BEGIN PRIVATE KEY-----", dry_run=True)


class WorkflowTest(unittest.TestCase):
    def test_given_workflow_when_read_then_it_is_dispatch_only_on_nb_route_and_dry_run_by_default(self):
        self.assertIn("workflow_dispatch:", WF)
        self.assertNotIn("push:", WF)
        self.assertIn("[self-hosted, nb-route]", WF)
        self.assertRegex(WF, r"dry_run:[\s\S]*?default: true")
        for a in ("inventory", "origin-cert", "dns-switch", "dns-revert"):
            self.assertIn(a, WF)

    def test_given_workflow_when_read_then_tokens_are_never_echoed(self):
        self.assertNotRegex(WF, r"echo[^\n]*secrets\.")
        self.assertNotRegex(WF, r"echo[^\n]*\$CF_")


class FakeLB(FakeCF):
    """FakeCF plus account pools/monitors and zone load balancers."""

    def __init__(self, pools=(), monitors=(), lbs=()):
        super().__init__([])
        self.zones[0]["account"] = {"id": "acct"}
        self.pools, self.monitors, self.lbs = list(pools), list(monitors), list(lbs)

    def list(self, path, params=None):
        if path.endswith("/load_balancers/pools"):
            return [dict(p) for p in self.pools]
        if path.endswith("/load_balancers/monitors"):
            return list(self.monitors)
        if path.endswith("/load_balancers"):
            return [dict(x) for x in self.lbs]
        return super().list(path, params)

    def call(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/monitors") and method == "POST":
            self.monitors.append(dict(body, id="mon-aws"))
            return {"result": {"id": "mon-aws"}}
        if path.endswith("/pools") and method == "POST":
            self.pools.append(dict(body, id="pool-" + body["name"]))
        elif "/pools/" in path and method == "PATCH":
            next(p for p in self.pools if p["id"] == path.rsplit("/", 1)[1]).update(body)
        elif path.endswith("/load_balancers") and method == "POST":
            self.lbs.append(dict(body, id="lb-new"))
        elif "/load_balancers/" in path and method == "PATCH":
            next(x for x in self.lbs if x["id"] == path.rsplit("/", 1)[1]).update(body)
        return {"result": {}}


HA_EXISTING = {"id": "p-ha-old", "name": "neonbee-app-fallback", "monitor": "mon-ha",
               "origins": [{"address": HA}]}


class LoadBalancerTest(unittest.TestCase):
    def test_given_no_aws_pools_when_setting_up_then_monitor_and_both_pools_are_created(self):
        cf = FakeLB(pools=[HA_EXISTING])
        res = cc.lb_pools([cf], "neonbee.app", dry_run=False)
        pools = {p["name"]: p for p in res["pools"]}
        self.assertEqual(sorted(pools[cc.AWS_POOL]["origins"]), sorted(cc.POOL_EIPS))
        self.assertEqual(pools[cc.AWS_POOL]["monitor"], "mon-aws")
        self.assertEqual(pools[cc.HA_POOL]["origins"], [HA])
        self.assertEqual(pools[cc.HA_POOL]["monitor"], "mon-ha")  # HA keeps the monitor it already uses
        mon = next(b for m, p, b in cf.calls if p.endswith("/monitors"))
        self.assertEqual((mon["path"], mon["expected_codes"], mon["interval"]), ("/__lb_health", "200", 60))
        self.assertNotIn("PATCH", [m for m, p, _ in cf.calls if "p-ha-old" in p])

    def test_given_pools_already_right_when_setting_up_again_then_nothing_is_written(self):
        cf = FakeLB(pools=[HA_EXISTING])
        cc.lb_pools([cf], "neonbee.app", dry_run=False)
        cf.calls.clear()
        res = cc.lb_pools([cf], "neonbee.app", dry_run=False)
        self.assertEqual(res["plan"], [])
        self.assertEqual([m for m, _, _ in cf.calls], [])

    def test_given_existing_lb_when_switching_then_default_is_aws_and_fallback_is_ha_with_prior_saved(self):
        cf = FakeLB(pools=[HA_EXISTING])
        cc.lb_pools([cf], "neonbee.app", dry_run=False)
        cf.lbs.append({"id": "lb1", "name": "api.neonbee.app", "default_pools": ["p-ha-old"],
                       "fallback_pool": "p-ha-old"})
        res = cc.lb_change([cf], "api.neonbee.app", ALLOWED, revert=False, dry_run=False)
        self.assertEqual(res["prior"]["default_pools"], ["p-ha-old"])
        self.assertEqual(cf.lbs[0]["default_pools"], ["pool-" + cc.AWS_POOL])
        self.assertEqual(cf.lbs[0]["fallback_pool"], "pool-" + cc.HA_POOL)
        res = cc.lb_change([cf], "api.neonbee.app", ALLOWED, revert=True, dry_run=False)
        self.assertEqual(cf.lbs[0]["default_pools"], ["pool-" + cc.HA_POOL])

    def test_given_host_without_lb_when_switching_then_lb_is_created_and_dns_untouched(self):
        cf = FakeLB(pools=[HA_EXISTING])
        cc.lb_pools([cf], "neonbee.app", dry_run=False)
        cc.lb_change([cf], "help.neonbee.app", ALLOWED, revert=False, dry_run=False)
        self.assertEqual(cf.lbs[0]["name"], "help.neonbee.app")
        self.assertFalse(any("dns_records" in p for _, p, _ in cf.calls))

    def test_given_missing_pools_or_excluded_host_when_switching_then_it_refuses(self):
        with self.assertRaises(cc.CutoverError):
            cc.lb_change([FakeLB()], "api.neonbee.app", ALLOWED, revert=False, dry_run=True)
        with self.assertRaises(cc.CutoverError):
            cc.lb_change([FakeLB()], "tools.neonbee.app", ALLOWED, revert=False, dry_run=True)

    def test_given_dry_run_when_switching_then_nothing_is_written(self):
        cf = FakeLB(pools=[HA_EXISTING])
        cc.lb_pools([cf], "neonbee.app", dry_run=False)
        cf.calls.clear()
        cc.lb_change([cf], "api.neonbee.app", ALLOWED, revert=False, dry_run=True)
        self.assertEqual([m for m, _, _ in cf.calls], [])


if __name__ == "__main__":
    unittest.main()
