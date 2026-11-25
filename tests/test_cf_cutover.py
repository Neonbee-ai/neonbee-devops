"""BDD: Cloudflare HA -> AWS cut-over script (infra/aws/cloudflare/cf_cutover.py)."""
import importlib.util
import json
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
        for h in ("dev-dashboard.neonbee.app", "pr-fix-x.skyoffice360.com", "dev-tools.neonbee.app",
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

    def test_given_token_without_dns_read_when_inventorying_then_other_token_is_used_and_run_continues(self):
        class NoDns(FakeCF):
            def list(self, path, params=None):
                if path.endswith("/dns_records"):
                    raise cc.CutoverError("10000: Authentication error")
                return super().list(path, params)
        bad, good = NoDns([]), FakeCF([rec("r1", HA, name="api.neonbee.app")], label="CF_PREVIEW_API_TOKEN")
        rep = cc.inventory([bad, good], EIPS, ALLOWED)
        self.assertEqual(rep[0]["dns_token"], "CF_PREVIEW_API_TOKEN")
        self.assertEqual(rep[0]["records"][0]["name"], "api.neonbee.app")
        rep = cc.inventory([NoDns([])], EIPS, ALLOWED)
        self.assertIn("unreadable", rep[0]["records"])

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

    def test_given_workflow_when_read_then_lb_token_is_passed_first(self):
        self.assertLess(WF.index("CF_LB_API_TOKEN"), WF.index("CF_SAAS_API_TOKEN"))

    def test_given_empty_tokens_when_verifying_then_status_only_is_reported(self):
        import contextlib, io
        env = {k: os.environ.pop(k, None) for k in ("CF_LB_API_TOKEN", "CF_SAAS_API_TOKEN", "CF_PREVIEW_API_TOKEN")}
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cc.main(["verify"]), 0)
            self.assertIn('"CF_LB_API_TOKEN": "empty"', buf.getvalue())
        finally:
            os.environ.update({k: v for k, v in env.items() if v})

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
            cc.lb_change([FakeLB()], "dev-dashboard.neonbee.app", ALLOWED | {"dev-dashboard.neonbee.app"}, revert=False, dry_run=True)

    def test_given_dry_run_when_switching_then_nothing_is_written(self):
        cf = FakeLB(pools=[HA_EXISTING])
        cc.lb_pools([cf], "neonbee.app", dry_run=False)
        cf.calls.clear()
        cc.lb_change([cf], "api.neonbee.app", ALLOWED, revert=False, dry_run=True)
        self.assertEqual([m for m, _, _ in cf.calls], [])


OLD = cc.OLD_ORIGIN_IP
AWSH = cc.AWS_ORIGIN_HOST
MON = {"id": "mon-1", "description": cc.AWS_MONITOR, **{k: v for k, v in cc.MONITOR_BODY.items()}}


def ha_pool(origins=None, monitor="mon-old", name="neonbee-ha-failover"):
    return {"id": "p-" + name, "name": name, "monitor": monitor, "enabled": True,
            "origins": origins if origins is not None else [
                {"name": "ha", "address": HA, "weight": 0.01, "enabled": True, "port": 443},
                {"name": "old", "address": OLD, "weight": 0.99, "enabled": True}]}


class FakeD(FakeLB):
    """FakeLB plus monitor PATCH/DELETE and pool health."""

    def __init__(self, pools=(), monitors=(), records=()):
        super().__init__(pools=pools, monitors=monitors)
        self.records = list(records)

    def call(self, method, path, params=None, body=None):
        if "/monitors/" in path and method in ("PATCH", "DELETE"):
            self.calls.append((method, path, body))
            mid = path.rsplit("/", 1)[1]
            if method == "PATCH":
                next(m for m in self.monitors if m["id"] == mid).update(body)
            else:
                self.monitors = [m for m in self.monitors if m["id"] != mid]
            return {"result": {}}
        if path.endswith("/health"):
            self.calls.append((method, path, body))
            return {"result": {"healthy": True, "pop_health": {
                "BOM": {"healthy": True, "origins": [{HA: {"healthy": True}}, {AWSH: {"healthy": False, "failure_reason": "HTTP status code 404"}}]},
                "LHR": {"healthy": True, "origins": [{HA: {"healthy": True}}, {AWSH: {"healthy": True}}]}}}}
        return super().call(method, path, params, body)


class DnsOnlyTest(unittest.TestCase):
    def test_given_other_host_when_planning_then_it_is_refused(self):
        for h in ("api.neonbee.app", "aws-origin.moonhive.in", "x.aws-origin.neonbee.app"):
            with self.assertRaises(cc.CutoverError):
                cc.plan_dns_only([], h, EIPS)

    def test_given_no_records_when_planning_then_one_dns_only_ttl60_a_per_eip_is_created(self):
        ops = cc.plan_dns_only([], AWSH, EIPS)
        self.assertEqual([o for o, _, _ in ops], ["create", "create"])
        for _, _, b in ops:
            self.assertEqual((b["type"], b["proxied"], b["ttl"], b["comment"]), ("A", False, 60, cc.ADDED))

    def test_given_our_proxied_record_when_planning_then_it_is_fixed_and_missing_eip_created(self):
        r = rec("r1", EIPS[0], name=AWSH, proxied=True, comment=cc.ADDED)
        ops = cc.plan_dns_only([r], AWSH, EIPS)
        self.assertEqual([o for o, _, _ in ops], ["update", "create"])
        self.assertEqual(ops[0][2], {"proxied": False, "ttl": 60})

    def test_given_foreign_or_non_a_record_when_planning_then_it_refuses(self):
        with self.assertRaises(cc.CutoverError):
            cc.plan_dns_only([rec("r1", "9.9.9.9", name=AWSH, comment=None)], AWSH, EIPS)
        with self.assertRaises(cc.CutoverError):
            cc.plan_dns_only([rec("r1", "x.example", name=AWSH, typ="CNAME", comment=cc.ADDED)], AWSH, EIPS)

    def test_given_dry_run_when_upserting_then_nothing_is_written(self):
        cf = FakeCF([])
        res = cc.dns_only_upsert([cf], AWSH, EIPS, dry_run=True)
        self.assertEqual([m for m, _, _ in cf.calls], [])
        self.assertEqual(len(res["plan"]), 2)

    def test_given_apply_when_upserting_twice_then_second_run_is_a_no_op_with_prior_and_after(self):
        cf = FakeCF([])
        first = cc.dns_only_upsert([cf], AWSH, EIPS, dry_run=False)
        self.assertEqual((first["prior"], len(first["after"])), ([], 2))
        cf.calls.clear()
        second = cc.dns_only_upsert([cf], AWSH, EIPS, dry_run=False)
        self.assertEqual(second["plan"], [])
        self.assertEqual([m for m, _, _ in cf.calls], [])
        self.assertNotIn("DELETE", [m for m, _, _ in cf.calls])


class MonitorTest(unittest.TestCase):
    def test_given_monitor_body_when_read_then_it_targets_dashboard_host_not_api(self):
        self.assertEqual(cc.MONITOR_BODY["header"], {"Host": ["dashboard.neonbee.app"]})
        self.assertEqual((cc.MONITOR_BODY["type"], cc.MONITOR_BODY["path"], cc.MONITOR_BODY["expected_codes"],
                          cc.MONITOR_BODY["interval"], cc.MONITOR_BODY["retries"], cc.MONITOR_BODY["timeout"],
                          cc.MONITOR_BODY["follow_redirects"], cc.MONITOR_BODY["allow_insecure"]),
                         ("https", "/__lb_health", "200", 60, 2, 5, False, True))

    def test_given_no_monitor_when_ensuring_then_it_is_created(self):
        cf = FakeD()
        res = cc.monitor_ensure([cf], "neonbee.app", dry_run=False)
        self.assertEqual((res["plan"], res["prior"]), ("create", None))
        self.assertEqual(res["after"]["description"], cc.AWS_MONITOR)

    def test_given_stale_host_header_when_ensuring_then_only_that_monitor_is_patched(self):
        other = {"id": "mon-other", "description": "something-else", "path": "/x"}
        stale = dict(MON, header={"Host": ["api.neonbee.app"]})
        cf = FakeD(monitors=[other, stale])
        res = cc.monitor_ensure([cf], "neonbee.app", dry_run=False)
        self.assertEqual(res["plan"], "update")
        self.assertEqual([(m, p.rsplit("/", 1)[1]) for m, p, _ in cf.calls], [("PATCH", "mon-1")])
        self.assertEqual(res["prior"]["header"], {"Host": ["api.neonbee.app"]})
        self.assertEqual(cf.monitors[0], other)

    def test_given_correct_monitor_or_dry_run_when_ensuring_then_nothing_is_written(self):
        cf = FakeD(monitors=[dict(MON)])
        self.assertEqual(cc.monitor_ensure([cf], "neonbee.app", dry_run=False)["plan"], "no-op")
        cf2 = FakeD()
        cc.monitor_ensure([cf2], "neonbee.app", dry_run=True)
        self.assertEqual(cf2.calls, [])


class PoolSwapTest(unittest.TestCase):
    def test_given_castle_rock_when_guarding_then_it_is_refused_before_any_write(self):
        with self.assertRaises(cc.CutoverError):
            cc.guard_pool("castle-rock")
        cf = FakeD(pools=[ha_pool(name="castle-rock")], monitors=[dict(MON)])
        with self.assertRaises(cc.CutoverError):
            cc._find_pool(cf, "acct", "castle-rock")
        self.assertEqual(cf.calls, [])
        with self.assertRaises(cc.CutoverError):
            cc.plan_pool_swap(ha_pool(name="castle-rock"), "mon-1")

    def test_given_old_origin_when_swapping_then_ha_001_and_aws_hostname_099_with_monitor(self):
        cf = FakeD(pools=[ha_pool(), ha_pool(name="castle-rock")], monitors=[dict(MON)])
        res = cc.pool_origin_swap([cf], "neonbee.app", dry_run=False)
        o = res["after"]["origins"]
        self.assertEqual([(x["address"], x["weight"], x["enabled"]) for x in o], [(HA, 0.01, True), (AWSH, 0.99, True)])
        self.assertEqual((o[0]["name"], o[0]["port"]), ("ha", 443))  # HA origin fields kept
        self.assertEqual((o[1]["name"], res["after"]["monitor"]), ("aws-origin", "mon-1"))
        self.assertEqual(res["prior"]["origins"][1]["address"], OLD)
        self.assertEqual([p for _, p, _ in cf.calls if "castle" in p], [])

    def test_given_swap_when_planned_then_new_origin_has_no_per_origin_host_header(self):
        want = cc.plan_pool_swap(ha_pool(), "mon-1")
        self.assertNotIn("header", want["origins"][1])

    def test_given_foreign_origin_or_missing_monitor_when_swapping_then_it_refuses(self):
        with self.assertRaises(cc.CutoverError):
            cc.plan_pool_swap(ha_pool(origins=[{"address": "1.2.3.4", "weight": 1}]), "mon-1")
        with self.assertRaises(cc.CutoverError):
            cc.plan_pool_swap(ha_pool(), None)

    def test_given_already_swapped_when_swapping_again_then_it_is_a_no_op(self):
        cf = FakeD(pools=[ha_pool()], monitors=[dict(MON)])
        cc.pool_origin_swap([cf], "neonbee.app", dry_run=False)
        cf.calls.clear()
        res = cc.pool_origin_swap([cf], "neonbee.app", dry_run=False)
        self.assertEqual(res["plan"], "no-op")
        self.assertEqual([m for m, _, _ in cf.calls], [])

    def test_given_dry_run_when_swapping_then_nothing_is_written_but_prior_is_reported(self):
        cf = FakeD(pools=[ha_pool()], monitors=[dict(MON)])
        res = cc.pool_origin_swap([cf], "neonbee.app", dry_run=True)
        self.assertEqual(cf.calls, [])
        self.assertEqual(res["prior"]["name"], "neonbee-ha-failover")


class PoolRestoreTest(unittest.TestCase):
    @staticmethod
    def b64(obj):
        import base64
        return base64.b64encode(json.dumps(obj).encode()).decode()

    def test_given_swap_result_when_restoring_then_prior_origins_and_monitor_come_back(self):
        cf = FakeD(pools=[ha_pool()], monitors=[dict(MON)])
        prior = cc.pool_origin_swap([cf], "neonbee.app", dry_run=False)["prior"]
        res = cc.pool_restore([cf], "neonbee.app", self.b64(prior), dry_run=False)
        self.assertEqual(res["after"]["origins"], prior["origins"])
        self.assertEqual(res["after"]["monitor"], "mon-old")

    def test_given_other_pool_or_foreign_origin_when_restoring_then_it_refuses_without_writing(self):
        cf = FakeD(pools=[ha_pool()], monitors=[dict(MON)])
        with self.assertRaises(cc.CutoverError):
            cc.pool_restore([cf], "neonbee.app", self.b64(ha_pool(name="castle-rock")), dry_run=False)
        with self.assertRaises(cc.CutoverError):
            cc.pool_restore([cf], "neonbee.app", self.b64(ha_pool(origins=[{"address": "1.2.3.4"}])), dry_run=False)
        with self.assertRaises(cc.CutoverError):
            cc.pool_restore([cf], "neonbee.app", "not-base64-json", dry_run=False)
        self.assertEqual(cf.calls, [])

    def test_given_dry_run_when_restoring_then_nothing_is_written(self):
        cf = FakeD(pools=[ha_pool(origins=[{"address": HA}, {"address": AWSH}])], monitors=[dict(MON)])
        res = cc.pool_restore([cf], "neonbee.app", self.b64(ha_pool()), dry_run=True)
        self.assertEqual((res["plan"], cf.calls), ("update", []))


class PoolHealthTest(unittest.TestCase):
    def test_given_pop_health_when_summarising_then_each_origin_has_healthy_and_failure_reason(self):
        cf = FakeD(pools=[ha_pool(), ha_pool(name="castle-rock")])
        res = cc.pool_health([cf], "neonbee.app")
        self.assertTrue(res["origins"][HA]["healthy"])
        self.assertTrue(res["origins"][AWSH]["healthy"])
        self.assertEqual(res["origins"][AWSH]["failure_reason"], "HTTP status code 404")
        self.assertEqual([m for m, _, _ in cf.calls], ["GET"])
        self.assertTrue(all("p-neonbee-ha-failover" in p for _, p, _ in cf.calls))

    def test_given_no_healthy_pop_when_summarising_then_origin_is_unhealthy(self):
        out = cc.summarize_health({"pop_health": {"BOM": {"origins": [{AWSH: {"healthy": False, "failure_reason": "timeout"}}]}}})
        self.assertEqual((out[AWSH]["healthy"], out[AWSH]["failure_reason"]), (False, "timeout"))


class MonitorDeleteTest(unittest.TestCase):
    def test_given_monitor_used_by_a_pool_when_deleting_then_it_refuses(self):
        cf = FakeD(pools=[ha_pool(monitor="mon-1")], monitors=[dict(MON)])
        with self.assertRaises(cc.CutoverError):
            cc.monitor_delete([cf], "neonbee.app", dry_run=False)
        self.assertEqual([m for m, _, _ in cf.calls], [])

    def test_given_unreferenced_monitor_when_deleting_then_only_it_is_deleted(self):
        other = {"id": "mon-other", "description": "keep"}
        cf = FakeD(pools=[ha_pool(monitor="mon-old")], monitors=[dict(MON), other])
        res = cc.monitor_delete([cf], "neonbee.app", dry_run=False)
        self.assertEqual(cf.monitors, [other])
        self.assertEqual((res["prior"]["id"], res["after"]), ("mon-1", None))

    def test_given_dry_run_or_absent_monitor_when_deleting_then_nothing_is_written(self):
        cf = FakeD(pools=[ha_pool()], monitors=[dict(MON)])
        self.assertEqual(cc.monitor_delete([cf], "neonbee.app", dry_run=True)["plan"], "delete")
        self.assertEqual(cf.calls, [])
        self.assertEqual(cc.monitor_delete([FakeD()], "neonbee.app", dry_run=False)["plan"], "no-op")


class LbPoolsEipsTest(unittest.TestCase):
    def test_given_eips_when_setting_up_pools_then_aws_pool_uses_them_not_pool_eips(self):
        cf = FakeLB(pools=[HA_EXISTING])
        res = cc.lb_pools([cf], "neonbee.app", dry_run=False, eips=EIPS)
        pools = {p["name"]: p for p in res["pools"]}
        self.assertEqual(sorted(pools[cc.AWS_POOL]["origins"]), sorted(EIPS))

    def test_given_source_when_read_then_main_passes_eips_to_lb_pools(self):
        src = (ROOT / "infra/aws/cloudflare/cf_cutover.py").read_text()
        self.assertIn('lb_pools(clients, a.host or "neonbee.app", not a.apply, eips)', src)


class OptionDWorkflowTest(unittest.TestCase):
    def test_given_workflow_when_read_then_new_actions_and_prior_b64_are_wired(self):
        for act in ("dns-only-upsert", "monitor-ensure", "pool-origin-swap", "pool-restore", "pool-health", "monitor-delete"):
            self.assertIn(act, WF)
        self.assertIn("prior_b64:", WF)
        self.assertIn("PRIOR_B64: ${{ inputs.prior_b64 }}", WF)


NODE_A, NODE_B = EIPS
SWAPPED = [{"name": "ha", "address": HA, "weight": 0.01, "enabled": True, "port": 443},
           {"name": "aws-origin", "address": AWSH, "weight": 0.99, "enabled": True}]


class PoolAwsNodesTest(unittest.TestCase):
    """Phase 2: HA out of neonbee-ha-failover; both slots = AWS node EIPs 0.5/0.5."""

    def test_given_option_d_pool_when_moving_to_aws_nodes_then_slots_are_both_node_eips_at_half_weight(self):
        cf = FakeD(pools=[ha_pool(origins=[dict(o) for o in SWAPPED], monitor="mon-1"), ha_pool(name="castle-rock")],
                   monitors=[dict(MON)])
        res = cc.pool_aws_nodes([cf], "neonbee.app", EIPS, dry_run=False)
        o = res["after"]["origins"]
        self.assertEqual([(x["name"], x["address"], x["weight"], x["enabled"]) for x in o],
                         [("aws-node-a", NODE_A, 0.5, True), ("aws-node-b", NODE_B, 0.5, True)])
        self.assertEqual(res["after"]["monitor"], "mon-1")
        self.assertNotIn(HA, [x["address"] for x in o])
        self.assertTrue(all("header" not in x for x in o))
        self.assertEqual(res["prior"]["origins"][1]["address"], AWSH)
        self.assertEqual([p for _, p, _ in cf.calls if "castle" in p], [])

    def test_given_dry_run_when_moving_to_aws_nodes_then_nothing_is_written(self):
        cf = FakeD(pools=[ha_pool(origins=[dict(o) for o in SWAPPED])], monitors=[dict(MON)])
        res = cc.pool_aws_nodes([cf], "neonbee.app", EIPS, dry_run=True)
        self.assertEqual((res["plan"], cf.calls), ("update", []))
        self.assertEqual(res["target"]["origins"][0]["address"], NODE_A)

    def test_given_already_on_nodes_when_moving_again_then_it_is_a_no_op(self):
        cf = FakeD(pools=[ha_pool(origins=[dict(o) for o in SWAPPED])], monitors=[dict(MON)])
        cc.pool_aws_nodes([cf], "neonbee.app", EIPS, dry_run=False)
        cf.calls.clear()
        self.assertEqual(cc.pool_aws_nodes([cf], "neonbee.app", EIPS, dry_run=False)["plan"], "no-op")
        self.assertEqual([m for m, _, _ in cf.calls], [])

    def test_given_castle_rock_wrong_eips_foreign_origin_or_no_monitor_then_it_refuses(self):
        with self.assertRaises(cc.CutoverError):
            cc.plan_pool_aws_nodes(ha_pool(name="castle-rock"), "mon-1")
        with self.assertRaises(cc.CutoverError):
            cc.plan_pool_aws_nodes(ha_pool(origins=SWAPPED), "mon-1", ("13.126.56.13", NODE_B))
        with self.assertRaises(cc.CutoverError):
            cc.plan_pool_aws_nodes(ha_pool(origins=[{"address": "1.2.3.4"}]), "mon-1")
        with self.assertRaises(cc.CutoverError):
            cc.plan_pool_aws_nodes(ha_pool(origins=SWAPPED), None)

    def test_given_aws_nodes_result_when_restoring_then_option_d_layout_comes_back(self):
        import base64
        cf = FakeD(pools=[ha_pool(origins=[dict(o) for o in SWAPPED], monitor="mon-1")], monitors=[dict(MON)])
        prior = cc.pool_aws_nodes([cf], "neonbee.app", EIPS, dry_run=False)["prior"]
        res = cc.pool_restore([cf], "neonbee.app", base64.b64encode(json.dumps(prior).encode()).decode(), dry_run=False)
        self.assertEqual([x["address"] for x in res["after"]["origins"]], [HA, AWSH])

    def test_given_workflow_when_read_then_new_actions_are_dispatchable(self):
        for a in ("pool-aws-nodes", "dns-only-delete"):
            self.assertIn(a, WF)


class FakeDD(FakeD):
    def __init__(self, pools=(), monitors=(), records=()):
        super().__init__(pools=pools, monitors=monitors, records=records)

    def list(self, path, params=None):
        if path.endswith("/dns_records"):
            name = (params or {}).get("name")
            return [dict(r) for r in self.records if not name or r["name"] == name]
        return super().list(path, params)

    def call(self, method, path, params=None, body=None):
        if method == "DELETE" and "/dns_records/" in path:
            self.calls.append((method, path, body))
            self.records = [r for r in self.records if r["id"] != path.rsplit("/", 1)[1]]
            return {"result": {}}
        return super().call(method, path, params, body)


class DnsOnlyDeleteTest(unittest.TestCase):
    def recs(self):
        return [rec("d1", NODE_A, name=AWSH, proxied=False, comment=cc.ADDED),
                rec("d2", NODE_B, name=AWSH, proxied=False, comment=cc.ADDED),
                rec("k1", HA, name="api.neonbee.app")]

    def nodes_pool(self):
        return ha_pool(origins=[{"name": "aws-node-a", "address": NODE_A}, {"name": "aws-node-b", "address": NODE_B}])

    def test_given_pool_off_aws_origin_when_deleting_then_only_its_tool_added_records_go(self):
        cf = FakeDD(pools=[self.nodes_pool()], monitors=[dict(MON)], records=self.recs())
        res = cc.dns_only_delete([cf], AWSH, dry_run=False)
        self.assertEqual(sorted(p.rsplit("/", 1)[1] for m, p, _ in cf.calls if m == "DELETE"), ["d1", "d2"])
        self.assertEqual(res["after"], [])
        self.assertEqual([r["id"] for r in cf.records], ["k1"])

    def test_given_pool_still_uses_aws_origin_when_deleting_then_it_refuses_without_writing(self):
        cf = FakeDD(pools=[ha_pool(origins=[dict(o) for o in SWAPPED])], monitors=[dict(MON)], records=self.recs())
        with self.assertRaises(cc.CutoverError):
            cc.dns_only_delete([cf], AWSH, dry_run=False)
        self.assertEqual([m for m, _, _ in cf.calls if m == "DELETE"], [])

    def test_given_other_host_or_foreign_record_when_deleting_then_it_refuses(self):
        with self.assertRaises(cc.CutoverError):
            cc.plan_dns_only_delete([], "api.neonbee.app", self.nodes_pool())
        with self.assertRaises(cc.CutoverError):
            cc.plan_dns_only_delete([rec("x", NODE_A, name=AWSH, proxied=False)], AWSH, self.nodes_pool())
        with self.assertRaises(cc.CutoverError):
            cc.plan_dns_only_delete([rec("x", NODE_A, name=AWSH, proxied=True, comment=cc.ADDED)], AWSH, self.nodes_pool())

    def test_given_dry_run_when_deleting_then_nothing_is_written(self):
        cf = FakeDD(pools=[self.nodes_pool()], monitors=[dict(MON)], records=self.recs())
        res = cc.dns_only_delete([cf], AWSH, dry_run=True)
        self.assertEqual(len(res["plan"]), 2)
        self.assertEqual([m for m, _, _ in cf.calls], [])


if __name__ == "__main__":
    unittest.main()


class ItvmTargetTest(unittest.TestCase):
    """Phase 3: website apex, Pulse and Free Tools go HA -> Internal Tools VM (never AWS)."""
    ITVM = (cc.ITVM_IP,)

    def test_given_itvm_host_when_switching_to_itvm_then_ha_record_is_edited_to_itvm(self):
        cf = FakeCF([rec("r1", HA, name="neonbee.ai")], zones=("neonbee.ai",))
        res = cc.dns_change([cf], "neonbee.ai", self.ITVM, set(), revert=False, dry_run=False)
        self.assertTrue(res["verified"])
        self.assertEqual([(r["id"], r["content"], r["proxied"]) for r in cf.records], [("r1", cc.ITVM_IP, True)])

    def test_given_switched_itvm_host_when_reverting_then_ha_is_restored(self):
        cf = FakeCF([rec("r1", HA, name="pulse.neonbee.app")])
        cc.dns_change([cf], "pulse.neonbee.app", self.ITVM, set(), revert=False, dry_run=False)
        res = cc.dns_change([cf], "pulse.neonbee.app", self.ITVM, set(), revert=True, dry_run=False)
        self.assertTrue(res["verified"])
        self.assertEqual([r["content"] for r in cf.records], [HA])

    def test_given_non_itvm_host_when_targeting_itvm_then_it_refuses(self):
        for h in ("dashboard.neonbee.app", "neonbee.store", "dev-dashboard.neonbee.app", "shop.moonhive.in"):
            with self.assertRaises(cc.CutoverError, msg=h):
                cc.check_host(h, {"dashboard.neonbee.app", "neonbee.store"}, self.ITVM)

    def test_given_itvm_mixed_with_aws_eips_when_switching_then_it_refuses(self):
        with self.assertRaises(cc.CutoverError):
            cc.check_host("neonbee.ai", set(), (cc.ITVM_IP, EIPS[0]))

    def test_given_aws_bundle_serves_tools_app_when_switching_to_aws_eips_then_it_is_allowed(self):
        self.assertEqual(cc.check_host("tools.neonbee.app", cc.bundle_hosts(), EIPS), "tools.neonbee.app")

    def test_given_tools_app_when_targeting_itvm_then_it_refuses(self):
        with self.assertRaises(cc.CutoverError):
            cc.check_host("tools.neonbee.app", {"tools.neonbee.app"}, self.ITVM)


class StoreSitesTest(unittest.TestCase):
    def test_given_aws_bundle_when_scanned_then_store_apex_and_saas_fallback_are_switchable(self):
        hosts = cc.bundle_hosts()
        self.assertIn("neonbee.store", hosts)
        self.assertIn("customers-fallback.neonbee.store", hosts)


class FakeSSL:
    """In-memory zone for SSL mode + http_config_settings entrypoint ruleset."""

    phase = "http_config_settings"

    def __init__(self, zone="neonbee.store", ssl="full", rules=None, records=(), lbs=(), pools=()):
        self.label, self.calls, self.ssl = "CF_LB_API_TOKEN", [], ssl
        self.zones = [{"id": "z1", "name": zone, "status": "active", "account": {"id": "acct"}}]
        self.rs = None if rules is None else {"id": "rs1", "rules": [dict(r) for r in rules]}
        self.records, self.lbs, self.pools, self.n = list(records), list(lbs), list(pools), 0

    def list(self, path, params=None):
        if path == "/zones":
            return self.zones
        if path.endswith("/dns_records"):
            return [dict(r) for r in self.records]
        if path.endswith("/load_balancers"):
            return self.lbs
        if path.endswith("/load_balancers/pools"):
            return self.pools
        raise AssertionError(path)

    def call(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/settings/ssl"):
            if method == "PATCH":
                self.ssl = body["value"]
            return {"result": {"value": self.ssl}}
        if path.endswith(f"/phases/{self.phase}/entrypoint"):
            if method == "GET":
                if self.rs is None:
                    raise cc.CutoverError("GET x failed (t): 10003: could not find entrypoint ruleset")
                return {"result": json.loads(json.dumps(self.rs))}
            self.rs = {"id": "rs-new", "rules": [dict(r, id="r-new") for r in body["rules"]]}
            return {"result": self.rs}
        if "/rulesets/" in path and method == "POST":
            self.n += 1
            self.rs["rules"].append(dict(body, id=f"r{self.n}"))
            return {"result": self.rs}
        if "/rulesets/" in path and method == "DELETE":
            rid = path.rsplit("/", 1)[1]
            self.rs["rules"] = [r for r in self.rs["rules"] if r["id"] != rid]
            return {"result": {"id": rid}}
        if "/rulesets/" in path and method == "PATCH":
            rid = path.rsplit("/", 1)[1]
            r = next(r for r in self.rs["rules"] if r["id"] == rid)
            r.clear(), r.update(dict(body, id=rid))
            return {"result": self.rs}
        raise AssertionError((method, path))

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


OTHER = {"id": "keep1", "action": "set_config", "action_parameters": {"rocket_loader": False},
         "expression": "true", "description": "someone else"}


class SslModeTest(unittest.TestCase):
    def test_given_dry_run_when_setting_strict_then_prior_is_reported_and_nothing_is_written(self):
        cf = FakeSSL()
        res = cc.ssl_mode([cf], "neonbee.store", "strict", True)
        self.assertEqual((res["prior"], res["after"], res["changed"]), ("full", "full", True))
        self.assertEqual(cf.writes(), [])

    def test_given_apply_when_setting_strict_then_zone_is_strict_and_prior_full_is_kept(self):
        cf = FakeSSL()
        res = cc.ssl_mode([cf], "neonbee.store", "strict", False)
        self.assertEqual((res["prior"], res["after"]), ("full", "strict"))
        self.assertEqual(cf.writes(), [("PATCH", "/zones/z1/settings/ssl", {"value": "strict"})])

    def test_given_zone_already_in_target_mode_when_applying_then_nothing_is_written(self):
        cf = FakeSSL(ssl="strict")
        self.assertFalse(cc.ssl_mode([cf], "neonbee.store", "strict", False)["changed"])
        self.assertEqual(cf.writes(), [])

    def test_given_zone_outside_allowlist_or_bad_mode_when_setting_then_it_refuses(self):
        for zone, mode in (("neonbee.ai", "strict"), ("moonhive.in", "full"), ("neonbee.store", "flexible")):
            cf = FakeSSL(zone=zone)
            with self.assertRaises(cc.CutoverError):
                cc.ssl_mode([cf], zone, mode, False)
            self.assertEqual(cf.writes(), [])


class SslKeepFullTest(unittest.TestCase):
    def test_given_no_entrypoint_when_applying_then_it_is_created_with_one_marked_rule(self):
        cf = FakeSSL(zone="neonbee.app")
        res = cc.ssl_keep_full([cf], "neonbee.app", "pulse.neonbee.app,command.neonbee.app", False)
        self.assertIsNone(res["prior_ruleset"])
        (m, path, body), = cf.writes()
        self.assertEqual((m, path), ("PUT", "/zones/z1/rulesets/phases/http_config_settings/entrypoint"))
        rule = body["rules"][0]
        self.assertEqual((rule["action"], rule["action_parameters"], rule["description"]),
                         ("set_config", {"ssl": "full"}, cc.SSL_FULL_MARK))
        self.assertEqual(rule["expression"], '(http.host in {"command.neonbee.app" "pulse.neonbee.app"})')

    def test_given_other_rules_when_applying_then_rule_is_added_and_others_are_kept(self):
        cf = FakeSSL(zone="neonbee.app", rules=[OTHER])
        res = cc.ssl_keep_full([cf], "neonbee.app", "pulse.neonbee.app", False)
        self.assertEqual([c[0] for c in cf.writes()], ["POST"])
        self.assertEqual([r["id"] for r in res["after_ruleset"]["rules"]], ["keep1", "r1"])
        self.assertEqual(res["prior_ruleset"]["rules"][0]["id"], "keep1")

    def test_given_existing_marked_rule_when_applying_then_only_it_is_patched(self):
        mine = dict(cc.ssl_full_rule(["pulse.neonbee.app"]), id="mine")
        cf = FakeSSL(zone="neonbee.app", rules=[OTHER, mine])
        res = cc.ssl_keep_full([cf], "neonbee.app", "pulse.neonbee.app,mobility.neonbee.app", False)
        (m, path, _), = cf.writes()
        self.assertEqual((m, path), ("PATCH", "/zones/z1/rulesets/rs1/rules/mine"))
        self.assertEqual(len(res["after_ruleset"]["rules"]), 2)
        self.assertIn("mobility.neonbee.app", res["after_ruleset"]["rules"][1]["expression"])

    def test_given_dry_run_when_planning_then_nothing_is_written(self):
        cf = FakeSSL(zone="neonbee.app", rules=[OTHER])
        self.assertEqual(cc.ssl_keep_full([cf], "neonbee.app", "pulse.neonbee.app", True)["plan"], "add-rule")
        self.assertEqual(cf.writes(), [])

    def test_given_bad_hosts_or_zone_when_planning_then_it_refuses_without_writing(self):
        for zone, hosts in (("neonbee.app", ""), ("neonbee.app", "x.neonbee.store"), ("neonbee.app", "*.neonbee.app"),
                            ("neonbee.ai", "neonbee.ai"), ("neonbee.app", "a b.neonbee.app")):
            cf = FakeSSL(zone=zone)
            with self.assertRaises(cc.CutoverError):
                cc.ssl_keep_full([cf], zone, hosts, False)
            self.assertEqual(cf.writes(), [])

    def test_given_two_marked_rules_when_applying_then_it_refuses(self):
        mine = dict(cc.ssl_full_rule(["pulse.neonbee.app"]), id="m1")
        cf = FakeSSL(zone="neonbee.app", rules=[mine, dict(mine, id="m2")])
        with self.assertRaises(cc.CutoverError):
            cc.ssl_keep_full([cf], "neonbee.app", "pulse.neonbee.app", False)
        self.assertEqual(cf.writes(), [])


class FakeCache(FakeSSL):
    phase = "http_request_cache_settings"


MARKED = dict(cc.cache_rule_body(), id="mine")


class CacheRuleTest(unittest.TestCase):
    def test_given_no_entrypoint_when_applying_then_one_marked_cache_rule_is_created(self):
        cf = FakeCache()
        cc.cache_rule([cf], "neonbee.store", False)
        (m, path, body), = cf.writes()
        self.assertEqual((m, path), ("PUT", "/zones/z1/rulesets/phases/http_request_cache_settings/entrypoint"))
        rule = body["rules"][0]
        self.assertEqual((rule["action"], rule["description"]), ("set_cache_settings", "nb:storefront-edge-cache"))
        self.assertEqual(rule["action_parameters"]["edge_ttl"], {"mode": "bypass_by_default"})
        self.assertEqual(rule["action_parameters"]["browser_ttl"], {"mode": "respect_origin"})
        self.assertNotIn("cache_key", rule["action_parameters"])

    def test_given_other_rules_when_applying_then_rule_is_added_and_others_are_kept(self):
        cf = FakeCache(rules=[OTHER])
        res = cc.cache_rule([cf], "neonbee.store", False)
        self.assertEqual([c[0] for c in cf.writes()], ["POST"])
        self.assertEqual([r["id"] for r in res["after_ruleset"]["rules"]], ["keep1", "r1"])

    def test_given_existing_marked_rule_when_applying_then_only_it_is_patched(self):
        cf = FakeCache(rules=[OTHER, MARKED])
        cc.cache_rule([cf], "neonbee.store", False)
        (m, path, _), = cf.writes()
        self.assertEqual((m, path), ("PATCH", "/zones/z1/rulesets/rs1/rules/mine"))

    def test_given_dry_run_when_planning_then_nothing_is_written(self):
        cf = FakeCache(rules=[OTHER])
        self.assertEqual(cc.cache_rule([cf], "neonbee.store", True)["plan"], "add-rule")
        self.assertEqual(cf.writes(), [])

    def test_given_zone_outside_allowlist_when_applying_then_it_refuses_without_writing(self):
        for zone in ("neonbee.app", "neonbee.ai", "moonhive.in"):
            cf = FakeCache(zone=zone)
            with self.assertRaises(cc.CutoverError):
                cc.cache_rule([cf], zone, False)
            self.assertEqual(cf.writes(), [])

    def test_given_two_marked_rules_when_applying_then_it_refuses(self):
        cf = FakeCache(rules=[MARKED, dict(MARKED, id="m2")])
        with self.assertRaises(cc.CutoverError):
            cc.cache_rule([cf], "neonbee.store", False)
        self.assertEqual(cf.writes(), [])

    def test_given_expression_when_read_then_dashboard_api_preview_and_personal_requests_are_excluded(self):
        for s in ('"/api/"', '"/_next/"', "sf_customer_token=", "sf_preview", "_rsc=", 'any(http.request.headers.names[*] == "rsc")',
                  'starts_with(http.host, "dev-")', 'starts_with(http.host, "pr-")', 'http.host ne "neonbee.store"'):
            self.assertIn(s, cc.CACHE_EXPRESSION)


class CacheRuleRevertTest(unittest.TestCase):
    def test_given_marked_rule_when_reverting_then_only_it_is_deleted(self):
        cf = FakeCache(rules=[OTHER, MARKED])
        res = cc.cache_rule_revert([cf], "neonbee.store", False)
        self.assertEqual(cf.writes(), [("DELETE", "/zones/z1/rulesets/rs1/rules/mine", None)])
        self.assertEqual([r["id"] for r in res["after_ruleset"]["rules"]], ["keep1"])

    def test_given_dry_run_when_reverting_then_nothing_is_written(self):
        cf = FakeCache(rules=[MARKED])
        self.assertEqual(cc.cache_rule_revert([cf], "neonbee.store", True)["plan"], "delete-rule")
        self.assertEqual(cf.writes(), [])

    def test_given_no_marked_rule_or_no_entrypoint_when_reverting_then_it_is_a_noop(self):
        for rules in (None, [OTHER]):
            cf = FakeCache(rules=rules)
            self.assertEqual(cc.cache_rule_revert([cf], "neonbee.store", False)["plan"], "noop")
            self.assertEqual(cf.writes(), [])

    def test_given_zone_outside_allowlist_when_reverting_then_it_refuses(self):
        with self.assertRaises(cc.CutoverError):
            cc.cache_rule_revert([FakeCache(zone="neonbee.app")], "neonbee.app", False)


class CacheWorkflowTest(unittest.TestCase):
    def test_given_workflow_when_read_then_cache_actions_are_wired(self):
        for s in ("cache-rule", "cache-rule-revert"):
            self.assertIn(s, WF)


class ZoneAuditTest(unittest.TestCase):
    def test_given_zone_when_auditing_then_proxied_records_lb_origins_ssl_and_rules_are_listed(self):
        recs = [rec("1", "3.110.17.10", name="a.neonbee.app"), rec("2", "x.example", name="b.neonbee.app", typ="CNAME"),
                rec("3", "1.2.3.4", name="grey.neonbee.app", proxied=False),
                rec("4", "v=spf1", name="neonbee.app", typ="TXT")]
        lbs = [{"name": "dashboard.neonbee.app", "proxied": True, "default_pools": ["p1"], "fallback_pool": "p1"}]
        pools = [{"id": "p1", "name": "neonbee-ha-failover", "origins": [{"name": "n1", "address": "3.110.17.10",
                                                                          "enabled": True}]},
                 {"id": "p2", "name": "castle-rock", "origins": [{"name": "x", "address": "9.9.9.9"}]}]
        cf = FakeSSL(zone="neonbee.app", rules=[OTHER], records=recs, lbs=lbs, pools=pools)
        out = cc.zone_audit([cf], "neonbee.app")
        self.assertEqual([r["name"] for r in out["proxied_records"]], ["a.neonbee.app", "b.neonbee.app"])
        self.assertEqual(out["ssl"], "full")
        (lb,) = out["load_balancers"]
        self.assertEqual([p["name"] for p in lb["pools"]], ["neonbee-ha-failover"])
        self.assertEqual(lb["pools"][0]["origins"][0]["address"], "3.110.17.10")
        self.assertEqual(out["config_rules"]["rules"][0]["id"], "keep1")
        self.assertEqual(cf.writes(), [])

    def test_given_no_entrypoint_when_auditing_then_config_rules_is_none(self):
        self.assertIsNone(cc.zone_audit([FakeSSL()], "neonbee.store")["config_rules"])


class ZoneAuditNoRulesPermTest(unittest.TestCase):
    def test_given_token_without_config_rules_read_when_auditing_then_rules_are_reported_unreadable(self):
        cf = FakeSSL(rules=[])
        orig = cf.call

        def call(method, path, params=None, body=None):
            if "/rulesets/" in path:
                raise cc.CutoverError("GET x failed (t): 10000: Authentication error")
            return orig(method, path, params, body)
        cf.call = call
        out = cc.zone_audit([cf], "neonbee.store")
        self.assertTrue(out["config_rules"].startswith("unreadable"))
        self.assertEqual(out["ssl"], "full")


class SslWorkflowTest(unittest.TestCase):
    def test_given_workflow_when_read_then_ssl_actions_and_inputs_are_wired(self):
        for s in ("zone-audit", "ssl-mode", "ssl-keep-full", "ssl_mode:", "hosts:", '--ssl-mode "$SSL_MODE"',
                  '--hosts "$HOSTS"'):
            self.assertIn(s, WF)
