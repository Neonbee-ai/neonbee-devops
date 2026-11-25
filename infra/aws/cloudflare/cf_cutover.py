#!/usr/bin/env python3
"""cf_cutover.py - Cloudflare side of the HA -> AWS prod cut-over (one hostname at a time).

Actions (run by .github/workflows/neonbee-cf-cutover.yml, tokens come from GH secrets, never printed):
  inventory          list zones, SSL mode, every DNS record on the HA IP or the AWS EIPs
  origin-cert        sign a locally made CSR with Cloudflare Origin CA for <zone> + *.<zone>
                     (the private key never leaves the operator's machine; output = public cert)
  dns-switch <host>  HA A record -> the two AWS EIPs (proxied, two A records)
  dns-revert <host>  back to the HA IP
  lb-inventory | lb-pools | lb-switch | lb-revert   Cloudflare Load Balancer path (new pools nb-aws-prod/nb-ha-fallback)
  Option D (reuse the existing pool neonbee-ha-failover; the account origin limit is 4, so no new pools):
  dns-only-upsert    --host aws-origin.<zone> --eips a,b : DNS-only (grey cloud) A records -> AWS EIPs
  monitor-ensure     create/patch the nb-aws-lb-health monitor (Host dashboard.neonbee.app)
  pool-origin-swap   neonbee-ha-failover origins -> HA 0.01 + hostname aws-origin.neonbee.app 0.99, monitor nb-aws-lb-health
  pool-restore       PATCH neonbee-ha-failover back from a pool-origin-swap result (--prior-b64 / PRIOR_B64)
  pool-health        read-only per-origin health of neonbee-ha-failover
  monitor-delete     delete nb-aws-lb-health only when no pool references it
  zone-audit <zone>  read-only: proxied A/AAAA/CNAME, LB hostnames + pool origins, SSL mode, config rules
  ssl-mode <zone> --ssl-mode full|strict   zone allowlist {neonbee.app, neonbee.store}; result holds prior
  ssl-keep-full <zone> --hosts a,b   upsert ONE http_config_settings rule (cf-cutover:ssl-full) ssl=full
  cache-rule <zone>  upsert ONE http_request_cache_settings rule (nb:storefront-edge-cache): storefront HTML
                     respects the origin Cache-Control (zone allowlist {neonbee.store})
  cache-rule-revert <zone>  delete only that rule (other rules never touched)

Safety rules (enforced here, covered by tests/test_cf_cutover.py):
  * only hostnames served by the AWS nb-nginx-sites bundle (server_name in infra/aws/nginx/sites)
  * never Dev/preview hosts (dev-*, pr-*, *.skyoffice360.com) or moonhive.in
  * never deletes a pre-existing record: switch edits the HA record's content and ADDS the second
    EIP record (comment "cf-cutover:added"); revert edits content back to HA and removes only the
    records this tool added
  * refuses anything unexpected (CNAME, AAAA, DNS-only record, unknown IPs) instead of guessing
  * dry_run (default) prints the plan and writes nothing
  * option D: the pool allowlist is exactly {neonbee-ha-failover} (any other pool, e.g. castle-rock, is a
    CutoverError before any write); DNS deletes only ever hit records carrying the ADDED marker; the monitor
    is deleted only when it is nb-aws-lb-health and no pool references it; writes return prior + after
"""
import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.cloudflare.com/client/v4"
HA_IP = "84.247.131.148"
DEFAULT_EIPS = ("3.110.17.10", "13.233.75.233")
ADDED = "cf-cutover:added"
WAS = "cf-cutover: was "
SKIP_ZONES = ("moonhive.in", "skyoffice360.com")
DENY = (re.compile(r"(^|\.)(dev|pr)-"), re.compile(r"(^|\.)dev\."), re.compile(r"skyoffice360\.com$"),
        re.compile(r"moonhive\.in$"), re.compile(r"preview"))
# Internal Tools VM target (phase 3): the website apex and Pulse leave HA for the ITVM, never AWS.
# Only these hosts, only with --eips exactly this IP; revert still goes back to HA.
# tools.neonbee.app (Free Tools = shell-fe-tools) is platform -> AWS bundle (user decision 2026-11-22).
ITVM_IP = "167.114.98.177"
ITVM_HOSTS = frozenset({"neonbee.ai", "pulse.neonbee.app"})
SITES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "nginx", "sites")


class CutoverError(Exception):
    pass


def bundle_hosts(sites_dir=SITES):
    """Hostnames the AWS nginx bundle serves (server_name lines, catch-all `_` excluded)."""
    hosts = set()
    for path in glob.glob(os.path.join(sites_dir, "*.conf")):
        with open(path) as fh:
            for m in re.finditer(r"server_name\s+([^;]+);", fh.read()):
                hosts.update(h for h in m.group(1).split() if h != "_")
    return hosts


def check_host(host, allowed, eips=None):
    host = host.strip().lower().rstrip(".")
    if eips is not None and ITVM_IP in eips:
        if tuple(eips) != (ITVM_IP,):
            raise CutoverError(f"{ITVM_IP} must be the only target (never mixed with AWS EIPs)")
        if host not in ITVM_HOSTS:
            raise CutoverError(f"{host}: not an Internal Tools VM host {sorted(ITVM_HOSTS)}")
        return host
    for rx in DENY:
        if rx.search(host):
            raise CutoverError(f"{host}: Dev/preview/excluded host - never touched")
    if host not in allowed:
        raise CutoverError(f"{host}: not served by the AWS nb-nginx-sites bundle")
    return host


class CF:
    """Minimal Cloudflare API v4 client (Bearer token)."""

    def __init__(self, token, label="token"):
        self.token, self.label = token, label

    def call(self, method, path, params=None, body=None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                out = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                out = json.loads(e.read().decode())
            except ValueError:
                out = {"success": False, "errors": [{"code": e.code, "message": e.reason}]}
        if not out.get("success"):
            errs = "; ".join(f"{x.get('code')}: {x.get('message')}" for x in out.get("errors", []))
            raise CutoverError(f"{method} {path} failed ({self.label}): {errs or 'unknown error'}")
        return out

    def list(self, path, params=None):
        items, page = [], 1
        while True:
            out = self.call("GET", path, dict(params or {}, page=page, per_page=100))
            items += out.get("result") or []
            info = out.get("result_info") or {}
            if page >= int(info.get("total_pages") or 1):
                return items
            page += 1


def zones_by_token(clients):
    """{zone_name: (client, zone)} - first token that sees a zone wins; skip moonhive/skyoffice."""
    seen = {}
    for cf in clients:
        try:
            zones = cf.list("/zones")
        except CutoverError as e:
            print(f"warn: {e}", file=sys.stderr)
            continue
        for z in zones:
            if z["name"] in SKIP_ZONES:
                continue
            if z["name"] not in seen:
                seen[z["name"]] = (cf, dict(z, _alts=[]))
            else:
                seen[z["name"]][1]["_alts"].append(cf)
    return seen


def try_tokens(cf, z, fn):
    """Run fn(client) with the zone's first token, then any other token that sees the zone.
    Returns (result, label) or raises the last error (with every token's failure)."""
    errs = []
    for c in [cf] + list(z.get("_alts") or []):
        try:
            return fn(c), c.label
        except CutoverError as e:
            errs.append(str(e))
    raise CutoverError(" | ".join(errs))


def zone_for(host, zones):
    bare = host[2:] if host.startswith("*.") else host
    best = max((n for n in zones if bare == n or bare.endswith("." + n)), key=len, default=None)
    if not best:
        raise CutoverError(f"{host}: no visible Cloudflare zone (token lacks Zone:Read for it?)")
    return zones[best]


def inventory(clients, eips, allowed):
    zones, report = zones_by_token(clients), []
    watch = {HA_IP, *eips}
    for name in sorted(zones):
        cf, z = zones[name]
        row = {"zone": name, "zone_id": z["id"], "token": cf.label, "status": z.get("status")}
        try:
            row["ssl"] = cf.call("GET", f"/zones/{z['id']}/settings/ssl")["result"]["value"]
        except CutoverError as e:
            row["ssl"] = f"unreadable ({e})"
        try:
            certs = cf.list("/certificates", {"zone_id": z["id"]})
            row["origin_ca_certs"] = [{"id": c["id"], "hostnames": c.get("hostnames"),
                                       "expires_on": c.get("expires_on")} for c in certs]
        except CutoverError as e:
            row["origin_ca_certs"] = f"unreadable ({e})"
        recs = []
        try:
            all_recs, row["dns_token"] = try_tokens(cf, z, lambda c: c.list(f"/zones/{z['id']}/dns_records"))
        except CutoverError as e:
            row["records"] = f"unreadable ({e})"
            report.append(row)
            continue
        for r in all_recs:
            if r.get("content") not in watch:
                continue
            excluded, in_bundle = None, r["name"] in allowed
            try:
                check_host(r["name"], allowed)
            except CutoverError as e:
                excluded = str(e)
            recs.append({"name": r["name"], "type": r["type"], "content": r["content"],
                         "proxied": r.get("proxied"), "ttl": r.get("ttl"), "id": r["id"],
                         "comment": r.get("comment"), "in_aws_bundle": in_bundle, "excluded": excluded})
        row["records"] = sorted(recs, key=lambda x: (x["name"], x["content"]))
        report.append(row)
    return report


def _host_records(cf, zone_id, host):
    return cf.list(f"/zones/{zone_id}/dns_records", {"name": host})


def plan_switch(records, eips):
    """Pure: list of (op, record_or_None, body) to move host from HA to the EIPs."""
    other = [r for r in records if r["type"] not in ("A", "TXT", "MX", "CAA")]
    if other:
        raise CutoverError("refusing: host has " + ",".join(sorted({r['type'] for r in other}))
                           + " records (only A on the HA IP is handled)")
    a = [r for r in records if r["type"] == "A"]
    if not a:
        raise CutoverError("refusing: host has no A record")
    contents = {r["content"] for r in a}
    unknown = contents - {HA_IP, *eips}
    if unknown:
        raise CutoverError(f"refusing: A record(s) on unexpected IP(s) {sorted(unknown)}")
    if any(not r.get("proxied") for r in a):
        raise CutoverError("refusing: DNS-only (unproxied) A record - AWS SG only admits Cloudflare")
    if contents == set(eips) and len(a) == len(eips):
        return []
    ts = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())
    ops = []
    missing = [ip for ip in eips if ip not in contents]
    ha = [r for r in a if r["content"] == HA_IP]
    if len(ha) > 1:
        raise CutoverError("refusing: more than one A record on the HA IP")
    if ha and not missing:
        raise CutoverError("refusing: HA and both EIPs already present (mixed state) - revert first")
    # ADD first (HA keeps serving while both exist), then flip the HA record's content.
    for ip in (missing[1:] if ha else missing):
        ops.append(("add", None, {"type": "A", "name": a[0]["name"], "content": ip, "proxied": True,
                                  "ttl": 1, "comment": f"{ADDED} {ts}"}))
    if ha:
        ops.append(("edit", ha[0], {"content": missing[0], "proxied": True,
                                     "comment": f"{WAS}{HA_IP} {ts}"}))
    return ops


def plan_revert(records, eips):
    a = [r for r in records if r["type"] == "A"]
    if not a:
        raise CutoverError("refusing: host has no A record")
    unknown = {r["content"] for r in a} - {HA_IP, *eips}
    if unknown:
        raise CutoverError(f"refusing: A record(s) on unexpected IP(s) {sorted(unknown)}")
    ts = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())
    ha = [r for r in a if r["content"] == HA_IP]
    added = [r for r in a if r["content"] in eips and (r.get("comment") or "").startswith(ADDED)]
    keep = [r for r in a if r["content"] in eips and r not in added]
    ops = []
    if not ha:
        # Prefer the record that originally held the HA IP; else any non-added record; else an added one.
        target = next((r for r in keep if (r.get("comment") or "").startswith(WAS)), None) \
            or (keep[0] if keep else added.pop(0))
        if target in keep:
            keep.remove(target)
        ops.append(("edit", target, {"content": HA_IP, "proxied": True,
                                     "comment": f"cf-cutover: reverted {ts}"}))
    if keep:
        raise CutoverError("refusing: EIP record(s) not created by this tool: "
                           + ",".join(r["content"] for r in keep) + " - revert by hand")
    ops += [("remove-added", r, None) for r in added]
    return ops


def apply(cf, zone_id, ops):
    for op, rec, body in ops:
        if op == "add":
            cf.call("POST", f"/zones/{zone_id}/dns_records", body=body)
        elif op == "edit":
            cf.call("PATCH", f"/zones/{zone_id}/dns_records/{rec['id']}", body=body)
        elif op == "remove-added":
            if not (rec.get("comment") or "").startswith(ADDED):
                raise CutoverError("internal: refusing to delete a record this tool did not add")
            cf.call("DELETE", f"/zones/{zone_id}/dns_records/{rec['id']}")


def describe(ops):
    out = []
    for op, rec, body in ops:
        if op == "add":
            out.append(f"ADD    A {body['name']} -> {body['content']} proxied")
        elif op == "edit":
            out.append(f"EDIT   A {rec['name']} {rec['content']} -> {body['content']} (id {rec['id']})")
        else:
            out.append(f"REMOVE A {rec['name']} {rec['content']} (added by cf-cutover, id {rec['id']})")
    return out


def dns_change(clients, host, eips, allowed, revert, dry_run):
    host = check_host(host, allowed, eips)
    cf, z = zone_for(host, zones_by_token(clients))
    before = _host_records(cf, z["id"], host)
    ops = (plan_revert if revert else plan_switch)(before, eips)
    result = {"host": host, "zone": z["name"], "action": "dns-revert" if revert else "dns-switch",
              "dry_run": dry_run, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "before": [{k: r.get(k) for k in ("id", "type", "content", "proxied", "ttl", "comment")}
                         for r in before], "plan": describe(ops)}
    if ops and not dry_run:
        apply(cf, z["id"], ops)
        after = _host_records(cf, z["id"], host)
        result["after"] = [{k: r.get(k) for k in ("id", "type", "content", "proxied")} for r in after]
        want = {HA_IP} if revert else set(eips)
        got = {r["content"] for r in after if r["type"] == "A"}
        result["verified"] = got == want
        if got != want:
            raise CutoverError(f"verify failed: A contents now {sorted(got)}, wanted {sorted(want)}")
    return result


def origin_cert(clients, zone, csr_pem, dry_run):
    zones = zones_by_token(clients)
    if zone not in zones:
        raise CutoverError(f"{zone}: no visible Cloudflare zone")
    cf, _ = zones[zone]
    body = {"hostnames": [zone, "*." + zone], "requested_validity": 5475,
            "request_type": "origin-rsa", "csr": csr_pem}
    if "PRIVATE KEY" in csr_pem:
        raise CutoverError("refusing: input contains a private key - pass the CSR only")
    if dry_run:
        return {"zone": zone, "dry_run": True, "hostnames": body["hostnames"]}
    res = cf.call("POST", "/certificates", body=body)["result"]
    return {"zone": zone, "dry_run": False, "id": res["id"], "hostnames": res.get("hostnames"),
            "expires_on": res.get("expires_on"), "certificate": res["certificate"]}


# ---------------------------------------------------------------- Cloudflare Load Balancer path
AWS_POOL, HA_POOL, AWS_MONITOR = "nb-aws-prod", "nb-ha-fallback", "nb-aws-lb-health"
POOL_EIPS = ("3.110.17.10", "13.233.75.233", "13.126.56.13", "13.234.180.150")
MONITOR_BODY = {"type": "https", "method": "GET", "path": "/__lb_health", "expected_codes": "200",
                "header": {"Host": ["dashboard.neonbee.app"]}, "interval": 60, "retries": 2, "timeout": 5,
                "follow_redirects": False, "allow_insecure": True, "description": AWS_MONITOR}


def _account(zone):
    acct = (zone.get("account") or {}).get("id")
    if not acct:
        raise CutoverError(f"{zone.get('name')}: zone has no account id")
    return acct


def lb_inventory(clients):
    zones, out, accounts = zones_by_token(clients), {"zones": [], "accounts": {}}, {}
    for name in sorted(zones):
        cf, z = zones[name]
        accounts.setdefault(_account(z), cf)
        try:
            lbs, _ = try_tokens(cf, z, lambda c: c.list(f"/zones/{z['id']}/load_balancers"))
        except CutoverError as e:
            lbs = f"unreadable ({e})"
        out["zones"].append({"zone": name, "load_balancers": lbs})
    for acct, cf in accounts.items():
        row = {}
        for kind in ("pools", "monitors"):
            try:
                row[kind] = cf.list(f"/accounts/{acct}/load_balancers/{kind}")
            except CutoverError as e:
                row[kind] = f"unreadable ({e})"
        out["accounts"][acct] = row
    return out


def plan_pools(pools, monitors, eips=POOL_EIPS):
    """Pure: ops to create/update the AWS + HA pools and the AWS monitor. Never edits other pools."""
    ops = []
    mon = next((m for m in monitors if m.get("description") == AWS_MONITOR), None)
    if not mon:
        ops.append(("monitor-create", None, dict(MONITOR_BODY)))
    ha_src = next((p for p in pools if p.get("name") != HA_POOL
                   and any(o.get("address") == HA_IP for o in p.get("origins") or [])), None)
    ha_monitor = (ha_src or {}).get("monitor") or (mon or {}).get("id") or "$aws_monitor"
    want = {AWS_POOL: ([{"name": "aws-" + ip.replace(".", "-"), "address": ip, "enabled": True, "weight": 1}
                        for ip in eips], (mon or {}).get("id") or "$aws_monitor"),
            HA_POOL: ([{"name": "ha-84-247-131-148", "address": HA_IP, "enabled": True, "weight": 1}],
                      ha_monitor)}
    for name, (origins, monitor) in want.items():
        body = {"name": name, "origins": origins, "monitor": monitor, "enabled": True,
                "minimum_origins": 1, "description": "cf-cutover " + name}
        cur = next((p for p in pools if p.get("name") == name), None)
        if not cur:
            ops.append(("pool-create", None, body))
        elif sorted(o["address"] for o in cur.get("origins") or []) != sorted(o["address"] for o in origins) \
                or cur.get("monitor") != monitor or not cur.get("enabled"):
            ops.append(("pool-update", cur, body))
    return ops


def lb_pools(clients, zone_name, dry_run, eips=POOL_EIPS):  # eips: --eips from the CLI
    zones = zones_by_token(clients)
    if zone_name not in zones:
        raise CutoverError(f"{zone_name}: no visible zone")
    cf, z = zones[zone_name]
    acct = _account(z)
    pools = cf.list(f"/accounts/{acct}/load_balancers/pools")
    monitors = cf.list(f"/accounts/{acct}/load_balancers/monitors")
    ops = plan_pools(pools, monitors, eips)
    res = {"account": acct, "dry_run": dry_run, "plan": [(o, (b or {}).get("name") or (b or {}).get("description"))
                                                           for o, _, b in ops]}
    if dry_run:
        return res
    mon_id = next((m["id"] for m in monitors if m.get("description") == AWS_MONITOR), None)
    for op, cur, body in ops:
        if op == "monitor-create":
            mon_id = cf.call("POST", f"/accounts/{acct}/load_balancers/monitors", body=body)["result"]["id"]
            continue
        if body.get("monitor") == "$aws_monitor":
            body["monitor"] = mon_id
        if op == "pool-create":
            cf.call("POST", f"/accounts/{acct}/load_balancers/pools", body=body)
        else:
            cf.call("PATCH", f"/accounts/{acct}/load_balancers/pools/{cur['id']}", body=body)
    res["pools"] = [{"id": p["id"], "name": p["name"], "origins": [o["address"] for o in p.get("origins") or []],
                     "monitor": p.get("monitor")}
                    for p in cf.list(f"/accounts/{acct}/load_balancers/pools") if p["name"] in (AWS_POOL, HA_POOL)]
    return res


def lb_change(clients, host, allowed, revert, dry_run):
    """Point host's LB at nb-aws-prod (fallback nb-ha-fallback), or back to HA. Creates the LB if the
    host has none (an LB overrides a same-name DNS record, which is left untouched)."""
    host = check_host(host, allowed)
    cf, z = zone_for(host, zones_by_token(clients))
    acct = _account(z)
    pools = {p["name"]: p["id"] for p in cf.list(f"/accounts/{acct}/load_balancers/pools")}
    if AWS_POOL not in pools or HA_POOL not in pools:
        raise CutoverError("pools missing - run lb-pools first")
    lbs = [lb for lb in cf.list(f"/zones/{z['id']}/load_balancers") if lb.get("name") == host]
    prior = lbs[0] if lbs else None
    default = [pools[HA_POOL]] if revert else [pools[AWS_POOL]]
    body = {"default_pools": default, "fallback_pool": pools[HA_POOL], "proxied": True,
            "steering_policy": "off", "enabled": True}
    res = {"host": host, "zone": z["name"], "action": "lb-revert" if revert else "lb-switch",
           "dry_run": dry_run, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "prior": prior}
    if prior and prior.get("default_pools") == default and prior.get("fallback_pool") == pools[HA_POOL] \
            and prior.get("enabled", True):
        res["plan"] = "no-op"
        return res
    if not prior and revert:
        raise CutoverError(f"{host}: no load balancer to revert")
    res["plan"] = ("update" if prior else "create") + f" default={default} fallback={pools[HA_POOL]}"
    if dry_run:
        return res
    if prior:
        cf.call("PATCH", f"/zones/{z['id']}/load_balancers/{prior['id']}", body=body)
    else:
        cf.call("POST", f"/zones/{z['id']}/load_balancers", body=dict(body, name=host, description="cf-cutover"))
    after = [lb for lb in cf.list(f"/zones/{z['id']}/load_balancers") if lb.get("name") == host]
    res["after"] = after[0] if after else None
    if not after or after[0].get("default_pools") != default:
        raise CutoverError(f"verify failed for {host}")
    res["verified"] = True
    return res


# ---------------------------------------------------------------- Option D: reuse neonbee-ha-failover
HA_FAILOVER_POOL = "neonbee-ha-failover"
POOL_ALLOWLIST = frozenset({HA_FAILOVER_POOL})
OLD_ORIGIN_IP = "68.183.86.248"
AWS_ORIGIN_HOST = "aws-origin.neonbee.app"
AWS_ORIGIN_RE = re.compile(r"^aws-origin\.(neonbee\.app|neonbee\.store)$")
POOL_ORIGIN_ADDRS = frozenset({HA_IP, OLD_ORIGIN_IP, AWS_ORIGIN_HOST, *DEFAULT_EIPS})
AWS_NODE_NAMES = ("aws-node-a", "aws-node-b")


def guard_pool(name):
    if name not in POOL_ALLOWLIST:
        raise CutoverError(f"{name}: pool not in the allowlist {sorted(POOL_ALLOWLIST)} - never touched")
    return name


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _acct_for(clients, zone_name):
    zones = zones_by_token(clients)
    if zone_name not in zones:
        raise CutoverError(f"{zone_name}: no visible zone")
    cf, z = zones[zone_name]
    return cf, z, _account(z)


def _find_pool(cf, acct, name=HA_FAILOVER_POOL):
    """The allowlisted pool, found by name. Other pools are listed by the API but never read or written."""
    guard_pool(name)
    pool = next((p for p in cf.list(f"/accounts/{acct}/load_balancers/pools") if p.get("name") == name), None)
    if not pool:
        raise CutoverError(f"{name}: pool not found")
    return pool


def _find_monitor(cf, acct):
    return next((m for m in cf.list(f"/accounts/{acct}/load_balancers/monitors")
                 if m.get("description") == AWS_MONITOR), None)


def plan_dns_only(records, host, eips):
    """Pure: ops to hold exactly one DNS-only ttl-60 A record per EIP on host. Refuses foreign records."""
    if not AWS_ORIGIN_RE.match(host or ""):
        raise CutoverError(f"{host}: only aws-origin.neonbee.app|neonbee.store may be managed")
    if not eips:
        raise CutoverError("no EIPs given")
    mine = [r for r in records if r.get("name") == host]
    for r in mine:
        if r.get("type") != "A":
            raise CutoverError(f"{host}: unexpected {r.get('type')} record - refusing")
        if r.get("comment") != ADDED:
            raise CutoverError(f"{host}: A record {r.get('content')} was not added by this tool - refusing")
    ops = []
    for ip in eips:
        cur = next((r for r in mine if r.get("content") == ip), None)
        body = {"type": "A", "name": host, "content": ip, "proxied": False, "ttl": 60, "comment": ADDED}
        if not cur:
            ops.append(("create", None, body))
        elif cur.get("proxied") or cur.get("ttl") != 60:
            ops.append(("update", cur, {"proxied": False, "ttl": 60}))
    return ops


def dns_only_upsert(clients, host, eips, dry_run):
    cf, z = zone_for(host, zones_by_token(clients))
    prior = _host_records(cf, z["id"], host)
    ops = plan_dns_only(prior, host, eips)
    res = {"action": "dns-only-upsert", "host": host, "zone": z["name"], "dry_run": dry_run, "time": _now(),
           "prior": prior, "plan": [(o, b.get("content") or r.get("content")) for o, r, b in ops]}
    if dry_run or not ops:
        res["after"] = prior
        return res
    for op, r, body in ops:
        if op == "create":
            cf.call("POST", f"/zones/{z['id']}/dns_records", body=body)
        else:
            cf.call("PATCH", f"/zones/{z['id']}/dns_records/{r['id']}", body=body)
    res["after"] = _host_records(cf, z["id"], host)
    return res


def monitor_ensure(clients, zone_name, dry_run):
    cf, z, acct = _acct_for(clients, zone_name)
    cur = _find_monitor(cf, acct)
    # deep copy: the saved prior must not change when the live monitor is patched (restore relies on it)
    res = {"action": "monitor-ensure", "account": acct, "dry_run": dry_run, "time": _now(),
           "prior": json.loads(json.dumps(cur)) if cur else cur}
    if cur and all(cur.get(k) == v for k, v in MONITOR_BODY.items()):
        res.update(plan="no-op", after=cur)
        return res
    res["plan"] = "update" if cur else "create"
    if dry_run:
        return res
    if cur:
        cf.call("PATCH", f"/accounts/{acct}/load_balancers/monitors/{cur['id']}", body=dict(MONITOR_BODY))
    else:
        cf.call("POST", f"/accounts/{acct}/load_balancers/monitors", body=dict(MONITOR_BODY))
    res["after"] = _find_monitor(cf, acct)
    return res


def _check_origins(origins, allowed=POOL_ORIGIN_ADDRS):
    bad = sorted({o.get("address") for o in origins or []} - set(allowed))
    if bad:
        raise CutoverError(f"unexpected origins {bad} - refusing")


def plan_pool_swap(pool, monitor_id):
    """Pure: target {origins, monitor} for neonbee-ha-failover, or None when already there."""
    guard_pool(pool.get("name"))
    if not monitor_id:
        raise CutoverError(f"monitor {AWS_MONITOR} does not exist - run monitor-ensure first")
    cur = pool.get("origins") or []
    _check_origins(cur)
    ha = next((o for o in cur if o.get("address") == HA_IP), {})
    ha_origin = dict(ha, name=ha.get("name") or "ha-84-247-131-148", address=HA_IP, weight=0.01, enabled=True)
    # No per-origin Host header: the pool is shared by the api/cdn/dashboard/sso LBs.
    aws_origin = {"name": "aws-origin", "address": AWS_ORIGIN_HOST, "weight": 0.99, "enabled": True}
    want = {"origins": [ha_origin, aws_origin], "monitor": monitor_id}
    norm = lambda os_: [(o.get("name"), o.get("address"), o.get("weight"), o.get("enabled"), o.get("header")) for o in os_]
    if norm(cur) == norm(want["origins"]) and pool.get("monitor") == monitor_id:
        return None
    return want


def pool_origin_swap(clients, zone_name, dry_run):
    cf, z, acct = _acct_for(clients, zone_name)
    pool = _find_pool(cf, acct)
    mon = _find_monitor(cf, acct)
    want = plan_pool_swap(pool, (mon or {}).get("id"))
    res = {"action": "pool-origin-swap", "account": acct, "pool": pool["name"], "dry_run": dry_run,
           "time": _now(), "prior": pool, "plan": "no-op" if want is None else "update"}
    if want is None:
        res["after"] = pool
        return res
    res["target"] = want
    if dry_run:
        return res
    cf.call("PATCH", f"/accounts/{acct}/load_balancers/pools/{pool['id']}", body=want)
    res["after"] = _find_pool(cf, acct)
    return res


def plan_pool_aws_nodes(pool, monitor_id, eips=DEFAULT_EIPS):
    """Pure: phase 2 target for neonbee-ha-failover = the two AWS node EIPs at 0.5/0.5 (HA out), or None.
    Slot order kept: HA slot -> eips[0], second slot -> eips[1]."""
    guard_pool(pool.get("name"))
    if not monitor_id:
        raise CutoverError(f"monitor {AWS_MONITOR} does not exist - run monitor-ensure first")
    if tuple(eips) != DEFAULT_EIPS:
        raise CutoverError(f"--eips must be exactly {','.join(DEFAULT_EIPS)} (the nb-serving node EIPs)")
    cur = pool.get("origins") or []
    _check_origins(cur)
    want = {"origins": [{"name": n, "address": ip, "weight": 0.5, "enabled": True}
                        for n, ip in zip(AWS_NODE_NAMES, eips)], "monitor": monitor_id}
    norm = lambda os_: [(o.get("name"), o.get("address"), o.get("weight"), o.get("enabled"), o.get("header")) for o in os_]
    if norm(cur) == norm(want["origins"]) and pool.get("monitor") == monitor_id:
        return None
    return want


def pool_aws_nodes(clients, zone_name, eips, dry_run):
    cf, z, acct = _acct_for(clients, zone_name)
    pool = _find_pool(cf, acct)
    mon = _find_monitor(cf, acct)
    want = plan_pool_aws_nodes(pool, (mon or {}).get("id"), eips)
    res = {"action": "pool-aws-nodes", "account": acct, "pool": pool["name"], "dry_run": dry_run,
           "time": _now(), "prior": pool, "plan": "no-op" if want is None else "update"}
    if want is None:
        res["after"] = pool
        return res
    res["target"] = want
    if dry_run:
        return res
    cf.call("PATCH", f"/accounts/{acct}/load_balancers/pools/{pool['id']}", body=want)
    res["after"] = _find_pool(cf, acct)
    return res


def plan_dns_only_delete(records, host, pool):
    """Pure: delete ops for the tool-added DNS-only A records on aws-origin.*, only once the pool no longer uses it."""
    if not AWS_ORIGIN_RE.match(host or ""):
        raise CutoverError(f"{host}: only aws-origin.neonbee.app|neonbee.store may be deleted")
    guard_pool(pool.get("name"))
    if any(o.get("address") == host for o in pool.get("origins") or []):
        raise CutoverError(f"{host}: still an origin of {pool.get('name')} - refusing")
    mine = [r for r in records if r.get("name") == host]
    for r in mine:
        if r.get("type") != "A" or r.get("proxied") or r.get("comment") != ADDED:
            raise CutoverError(f"{host}: record {r.get('type')} {r.get('content')} is not a tool-added DNS-only A - refusing")
    return [("delete", r, None) for r in mine]


def dns_only_delete(clients, host, dry_run):
    cf, z = zone_for(host, zones_by_token(clients))
    lcf, _, acct = _acct_for(clients, "neonbee.app")
    pool = _find_pool(lcf, acct)
    prior = _host_records(cf, z["id"], host)
    ops = plan_dns_only_delete(prior, host, pool)
    res = {"action": "dns-only-delete", "host": host, "zone": z["name"], "dry_run": dry_run, "time": _now(),
           "prior": prior, "plan": [(o, r.get("content")) for o, r, _ in ops]}
    if dry_run or not ops:
        res["after"] = prior
        return res
    for _, r, _ in ops:
        cf.call("DELETE", f"/zones/{z['id']}/dns_records/{r['id']}")
    res["after"] = _host_records(cf, z["id"], host)
    return res


def pool_restore(clients, zone_name, prior_b64, dry_run):
    import base64
    try:
        prior = json.loads(base64.b64decode(prior_b64).decode())
    except (ValueError, TypeError) as e:
        raise CutoverError(f"prior_b64 is not base64 JSON: {e}")
    if not isinstance(prior, dict) or prior.get("name") != HA_FAILOVER_POOL:
        raise CutoverError(f"prior pool must be {HA_FAILOVER_POOL}")
    guard_pool(prior["name"])
    _check_origins(prior.get("origins"))
    if not prior.get("origins"):
        raise CutoverError("prior pool has no origins - refusing")
    cf, z, acct = _acct_for(clients, zone_name)
    cur = _find_pool(cf, acct)
    if prior.get("id") and prior["id"] != cur["id"]:
        raise CutoverError("prior pool id does not match the current pool")
    want = {"origins": prior["origins"], "monitor": prior.get("monitor")}
    res = {"action": "pool-restore", "account": acct, "pool": cur["name"], "dry_run": dry_run,
           "time": _now(), "prior": cur, "target": want}
    if cur.get("origins") == want["origins"] and cur.get("monitor") == want["monitor"]:
        res.update(plan="no-op", after=cur)
        return res
    res["plan"] = "update"
    if dry_run:
        return res
    cf.call("PATCH", f"/accounts/{acct}/load_balancers/pools/{cur['id']}", body=want)
    res["after"] = _find_pool(cf, acct)
    return res


def summarize_health(result):
    """Pure: per-origin {healthy, failure_reason} from a pool health result (any POP healthy = healthy)."""
    out = {}
    for pop in ((result or {}).get("pop_health") or {}).values():
        for o in pop.get("origins") or []:
            for addr, st in o.items():
                row = out.setdefault(addr, {"healthy": False, "failure_reason": None, "pops": 0, "pops_healthy": 0})
                row["pops"] += 1
                if st.get("healthy"):
                    row["healthy"] = True
                    row["pops_healthy"] += 1
                elif st.get("failure_reason"):
                    row["failure_reason"] = st["failure_reason"]
    return out


def pool_health(clients, zone_name):
    cf, z, acct = _acct_for(clients, zone_name)
    pool = _find_pool(cf, acct)
    out = cf.call("GET", f"/accounts/{acct}/load_balancers/pools/{pool['id']}/health")
    return {"action": "pool-health", "account": acct, "pool": pool["name"], "time": _now(),
            "healthy": (out.get("result") or {}).get("healthy"),
            "origins": summarize_health(out.get("result"))}


def monitor_delete(clients, zone_name, dry_run):
    cf, z, acct = _acct_for(clients, zone_name)
    mon = _find_monitor(cf, acct)
    res = {"action": "monitor-delete", "account": acct, "dry_run": dry_run, "time": _now(), "prior": mon}
    if not mon:
        res.update(plan="no-op", after=None)
        return res
    users = [p.get("monitor") for p in cf.list(f"/accounts/{acct}/load_balancers/pools")]
    if mon["id"] in users:
        raise CutoverError(f"monitor {AWS_MONITOR} is still referenced by a pool - not deleted")
    res["plan"] = "delete"
    if dry_run:
        return res
    cf.call("DELETE", f"/accounts/{acct}/load_balancers/monitors/{mon['id']}")
    res["after"] = _find_monitor(cf, acct)
    return res


# ---- SSL mode (Full -> Full (strict)) with per-host Full exceptions -------------------------------
SSL_ZONES = frozenset({"neonbee.app", "neonbee.store"})
SSL_MODES = ("full", "strict")
SSL_FULL_MARK = "cf-cutover:ssl-full"
CFG_PHASE = "http_config_settings"
HOST_RE = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


def guard_ssl_zone(zone_name):
    if zone_name not in SSL_ZONES:
        raise CutoverError(f"{zone_name}: zone not in the SSL allowlist {sorted(SSL_ZONES)} - never touched")
    return zone_name


def _ssl_zone(clients, zone_name):
    guard_ssl_zone(zone_name)
    zones = zones_by_token(clients)
    if zone_name not in zones:
        raise CutoverError(f"{zone_name}: no visible zone")
    return zones[zone_name]


def _cfg_entrypoint(cf, zone_id, phase=CFG_PHASE):
    """The zone's entrypoint ruleset of a phase (default http_config_settings), or None when it has none yet."""
    try:
        return cf.call("GET", f"/zones/{zone_id}/rulesets/phases/{phase}/entrypoint")["result"]
    except CutoverError as e:
        if re.search(r"\b10003\b|not found|could not find", str(e), re.I):
            return None
        raise


def zone_audit(clients, zone_name):
    """Read-only: proxied A/AAAA/CNAME records, LB hostnames + pool origins, SSL mode, config rules."""
    cf, z = _ssl_zone(clients, zone_name)
    zid = z["id"]
    out = {"zone": zone_name, "zone_id": zid, "token": cf.label,
           "ssl": cf.call("GET", f"/zones/{zid}/settings/ssl")["result"]["value"]}
    recs = cf.list(f"/zones/{zid}/dns_records")
    out["proxied_records"] = sorted(({"name": r["name"], "type": r["type"], "content": r["content"]}
                                     for r in recs if r.get("proxied") and r.get("type") in ("A", "AAAA", "CNAME")),
                                    key=lambda x: (x["name"], x["type"], x["content"]))
    try:
        lbs = cf.list(f"/zones/{zid}/load_balancers")
    except CutoverError as e:
        lbs, out["load_balancers"] = [], f"unreadable ({e})"
    if lbs:
        pools = {}
        try:
            pools = {p["id"]: p for p in cf.list(f"/accounts/{_account(z)}/load_balancers/pools")}
        except CutoverError as e:
            out["pools_error"] = str(e)
        rows = []
        for lb in lbs:
            ids = list(dict.fromkeys(list(lb.get("default_pools") or []) + [lb.get("fallback_pool")]))
            rows.append({"hostname": lb.get("name"), "proxied": lb.get("proxied"), "pools": [
                {"id": i, "name": (pools.get(i) or {}).get("name"),
                 "origins": [{"name": o.get("name"), "address": o.get("address"), "enabled": o.get("enabled"),
                              "host_header": ((o.get("header") or {}).get("Host"))}
                             for o in (pools.get(i) or {}).get("origins") or []]} for i in ids if i]})
        out["load_balancers"] = rows
    elif "load_balancers" not in out:
        out["load_balancers"] = []
    try:
        rs = _cfg_entrypoint(cf, zid)
        out["config_rules"] = None if rs is None else {"id": rs.get("id"), "rules": rs.get("rules") or []}
    except CutoverError as e:  # token without Config Rules read: report, keep the read-only audit going
        out["config_rules"] = f"unreadable ({e})"
    return out


def ssl_mode(clients, zone_name, mode, dry_run):
    """Set the zone SSL mode to full|strict. Result carries the prior value (revert = the other mode)."""
    if mode not in SSL_MODES:
        raise CutoverError(f"ssl_mode {mode!r}: only {list(SSL_MODES)}")
    cf, z = _ssl_zone(clients, zone_name)
    path = f"/zones/{z['id']}/settings/ssl"
    prior = cf.call("GET", path)["result"]["value"]
    res = {"zone": zone_name, "prior": prior, "target": mode, "dry_run": dry_run, "at": _now(),
           "changed": prior != mode}
    if dry_run or prior == mode:
        res["after"] = prior
        return res
    cf.call("PATCH", path, body={"value": mode})
    res["after"] = cf.call("GET", path)["result"]["value"]
    return res


def parse_hosts(raw, zone_name):
    hosts = sorted({h.strip().lower() for h in (raw or "").split(",") if h.strip()})
    if not hosts:
        raise CutoverError("ssl-keep-full needs --hosts a,b (explicit host list)")
    for h in hosts:
        if not HOST_RE.match(h) or "*" in h:
            raise CutoverError(f"{h}: not a plain hostname")
        if not (h == zone_name or h.endswith("." + zone_name)):
            raise CutoverError(f"{h}: not in zone {zone_name}")
    return hosts


def ssl_full_rule(hosts):
    return {"action": "set_config", "action_parameters": {"ssl": "full"},
            "expression": "(http.host in {" + " ".join(f'"{h}"' for h in hosts) + "})",
            "description": SSL_FULL_MARK, "enabled": True}


def ssl_keep_full(clients, zone_name, raw_hosts, dry_run):
    """Upsert ONE config rule (description SSL_FULL_MARK) that keeps the listed hosts on SSL Full.
    Never removes or edits any other rule of the ruleset."""
    hosts = parse_hosts(raw_hosts, zone_name)
    cf, z = _ssl_zone(clients, zone_name)
    zid, rule = z["id"], ssl_full_rule(hosts)
    prior = _cfg_entrypoint(cf, zid)
    mine = [r for r in (prior or {}).get("rules") or [] if r.get("description") == SSL_FULL_MARK]
    if len(mine) > 1:
        raise CutoverError(f"{len(mine)} rules carry {SSL_FULL_MARK} - fix by hand, refusing to guess")
    if prior is None:
        op = {"op": "create-entrypoint", "path": f"/zones/{zid}/rulesets/phases/{CFG_PHASE}/entrypoint"}
    elif mine:
        op = {"op": "patch-rule", "path": f"/zones/{zid}/rulesets/{prior['id']}/rules/{mine[0]['id']}"}
    else:
        op = {"op": "add-rule", "path": f"/zones/{zid}/rulesets/{prior['id']}/rules"}
    res = {"zone": zone_name, "hosts": hosts, "rule": rule, "plan": op["op"], "prior_ruleset": prior,
           "dry_run": dry_run, "at": _now()}
    if dry_run:
        return res
    if op["op"] == "create-entrypoint":
        cf.call("PUT", op["path"], body={"rules": [rule]})
    elif op["op"] == "patch-rule":
        cf.call("PATCH", op["path"], body=rule)
    else:
        cf.call("POST", op["path"], body=rule)
    after = _cfg_entrypoint(cf, zid) or {}
    before_ids = {r.get("id") for r in (prior or {}).get("rules") or []}
    after_ids = {r.get("id") for r in after.get("rules") or []}
    if not before_ids <= after_ids:
        raise CutoverError(f"rules disappeared after write: {sorted(before_ids - after_ids)}")
    res["after_ruleset"] = after
    return res


CACHE_ZONES = frozenset({"neonbee.store"})
CACHE_PHASE = "http_request_cache_settings"
CACHE_MARK = "nb:storefront-edge-cache"
# Origin decides: only responses the storefront stamps `public, s-maxage` are stored; private/no-store and
# anything without Cache-Control bypass. Host stays in the default cache key (never customised here).
CACHE_EXPRESSION = (
    '(http.request.method in {"GET" "HEAD"}'
    ' and not starts_with(http.request.uri.path, "/api/")'
    ' and not starts_with(http.request.uri.path, "/_next/")'
    ' and not http.cookie contains "sf_customer_token="'
    ' and not http.cookie contains "sf_preview"'
    ' and not http.request.uri.query contains "preview="'
    ' and not http.request.uri.query contains "_rsc="'
    ' and not "rsc" in http.request.headers.names'
    ' and not starts_with(http.host, "dev-")'
    ' and not starts_with(http.host, "pr-")'
    ' and http.host ne "neonbee.store"'
    ' and http.host ne "www.neonbee.store")')


def cache_rule_body():
    return {"action": "set_cache_settings",
            "action_parameters": {"cache": True,
                                  "edge_ttl": {"mode": "bypass_by_default"},
                                  "browser_ttl": {"mode": "respect_origin"},
                                  "serve_stale": {"disable_stale_while_updating": False},
                                  "origin_cache_control": True,
                                  "cache_deception_armor": True},
            "expression": CACHE_EXPRESSION, "description": CACHE_MARK, "enabled": True}


def _cache_zone(clients, zone_name):
    if zone_name not in CACHE_ZONES:
        raise CutoverError(f"{zone_name}: zone not in the cache-rule allowlist {sorted(CACHE_ZONES)} - never touched")
    zones = zones_by_token(clients)
    if zone_name not in zones:
        raise CutoverError(f"{zone_name}: no visible zone")
    return zones[zone_name]


def _marked(ruleset):
    mine = [r for r in (ruleset or {}).get("rules") or [] if r.get("description") == CACHE_MARK]
    if len(mine) > 1:
        raise CutoverError(f"{len(mine)} rules carry {CACHE_MARK} - fix by hand, refusing to guess")
    return mine


def _kept(before, after):
    lost = {r.get("id") for r in (before or {}).get("rules") or []} - {r.get("id") for r in (after or {}).get("rules") or []}
    return lost


def cache_rule(clients, zone_name, dry_run):
    """Upsert ONE cache rule (description CACHE_MARK). Never removes or edits any other rule."""
    cf, z = _cache_zone(clients, zone_name)
    zid, rule = z["id"], cache_rule_body()
    prior = _cfg_entrypoint(cf, zid, CACHE_PHASE)
    mine = _marked(prior)
    if prior is None:
        op = {"op": "create-entrypoint", "path": f"/zones/{zid}/rulesets/phases/{CACHE_PHASE}/entrypoint"}
    elif mine:
        op = {"op": "patch-rule", "path": f"/zones/{zid}/rulesets/{prior['id']}/rules/{mine[0]['id']}"}
    else:
        op = {"op": "add-rule", "path": f"/zones/{zid}/rulesets/{prior['id']}/rules"}
    res = {"zone": zone_name, "rule": rule, "plan": op["op"], "prior_ruleset": prior, "dry_run": dry_run, "at": _now()}
    if dry_run:
        return res
    if op["op"] == "create-entrypoint":
        cf.call("PUT", op["path"], body={"rules": [rule]})
    elif op["op"] == "patch-rule":
        cf.call("PATCH", op["path"], body=rule)
    else:
        cf.call("POST", op["path"], body=rule)
    after = _cfg_entrypoint(cf, zid, CACHE_PHASE) or {}
    if _kept(prior, after):
        raise CutoverError(f"rules disappeared after write: {sorted(_kept(prior, after))}")
    res["after_ruleset"] = after
    return res


def cache_rule_revert(clients, zone_name, dry_run):
    """Delete only the CACHE_MARK rule. No marked rule -> nothing to do."""
    cf, z = _cache_zone(clients, zone_name)
    zid = z["id"]
    prior = _cfg_entrypoint(cf, zid, CACHE_PHASE)
    mine = _marked(prior)
    res = {"zone": zone_name, "plan": "delete-rule" if mine else "noop", "prior_ruleset": prior,
           "dry_run": dry_run, "at": _now()}
    if dry_run or not mine:
        return res
    cf.call("DELETE", f"/zones/{zid}/rulesets/{prior['id']}/rules/{mine[0]['id']}")
    after = _cfg_entrypoint(cf, zid, CACHE_PHASE) or {}
    if _kept({"rules": [r for r in prior["rules"] if r is not mine[0]]}, after):
        raise CutoverError("an unrelated rule disappeared after the delete")
    res["after_ruleset"] = after
    return res


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["inventory", "origin-cert", "dns-switch", "dns-revert",
                                       "lb-inventory", "lb-pools", "lb-switch", "lb-revert", "verify",
                                       "dns-only-upsert", "monitor-ensure", "pool-origin-swap",
                                       "pool-restore", "pool-health", "monitor-delete",
                                       "pool-aws-nodes", "dns-only-delete",
                                       "zone-audit", "ssl-mode", "ssl-keep-full",
                                       "cache-rule", "cache-rule-revert"])
    p.add_argument("--host", default="")
    p.add_argument("--eips", default=",".join(DEFAULT_EIPS))
    p.add_argument("--csr-b64", default=os.environ.get("CSR_B64", ""))
    p.add_argument("--prior-b64", default=os.environ.get("PRIOR_B64", ""))
    p.add_argument("--ssl-mode", default="")
    p.add_argument("--hosts", default="")
    p.add_argument("--out", default="cf-out")
    p.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    a = p.parse_args(argv)
    clients = [CF(os.environ[n], n) for n in ("CF_LB_API_TOKEN", "CF_SAAS_API_TOKEN", "CF_PREVIEW_API_TOKEN") if os.environ.get(n)]
    if not clients and a.action != "verify":
        print("no Cloudflare token in env", file=sys.stderr)
        return 2
    if a.action == "verify":  # status only, never the token
        res = {}
        for n in ("CF_LB_API_TOKEN", "CF_SAAS_API_TOKEN", "CF_PREVIEW_API_TOKEN"):
            v = os.environ.get(n, "")
            if not v:
                res[n] = "empty"
                continue
            try:
                r = CF(v, n).call("GET", "/user/tokens/verify")["result"]
                res[n] = {"status": r.get("status"), "expires_on": r.get("expires_on")}
            except CutoverError as e:
                res[n] = "invalid: " + str(e).split("): ", 1)[-1]
        print(json.dumps(res))
        return 0
    eips = tuple(x.strip() for x in a.eips.split(",") if x.strip())
    allowed = bundle_hosts()
    os.makedirs(a.out, exist_ok=True)
    def run(clients):
        if a.action == "inventory":
            res = inventory(clients, eips, allowed)
        elif a.action == "origin-cert":
            import base64
            res = origin_cert(clients, a.host, base64.b64decode(a.csr_b64).decode(), not a.apply)
            if res.get("certificate"):
                with open(os.path.join(a.out, f"{a.host}.origin.crt"), "w") as fh:
                    fh.write(res.pop("certificate"))
        elif a.action == "lb-inventory":
            res = lb_inventory(clients)
        elif a.action == "lb-pools":
            res = lb_pools(clients, a.host or "neonbee.app", not a.apply, eips)
        elif a.action == "dns-only-upsert":
            res = dns_only_upsert(clients, a.host, eips, not a.apply)
        elif a.action == "monitor-ensure":
            res = monitor_ensure(clients, "neonbee.app", not a.apply)
        elif a.action == "pool-origin-swap":
            res = pool_origin_swap(clients, "neonbee.app", not a.apply)
        elif a.action == "pool-aws-nodes":
            res = pool_aws_nodes(clients, "neonbee.app", eips, not a.apply)
        elif a.action == "dns-only-delete":
            res = dns_only_delete(clients, a.host, not a.apply)
        elif a.action == "pool-restore":
            res = pool_restore(clients, "neonbee.app", a.prior_b64, not a.apply)
        elif a.action == "zone-audit":
            res = zone_audit(clients, a.host)
        elif a.action == "ssl-mode":
            res = ssl_mode(clients, a.host, a.ssl_mode, not a.apply)
        elif a.action == "ssl-keep-full":
            res = ssl_keep_full(clients, a.host, a.hosts, not a.apply)
        elif a.action == "cache-rule":
            res = cache_rule(clients, a.host, not a.apply)
        elif a.action == "cache-rule-revert":
            res = cache_rule_revert(clients, a.host, not a.apply)
        elif a.action == "pool-health":
            res = pool_health(clients, "neonbee.app")
        elif a.action == "monitor-delete":
            res = monitor_delete(clients, "neonbee.app", not a.apply)
        elif a.action in ("lb-switch", "lb-revert"):
            res = lb_change(clients, a.host, allowed, a.action == "lb-revert", not a.apply)
        else:
            res = dns_change(clients, a.host, eips, allowed, a.action == "dns-revert", not a.apply)
        return res

    try:
        try:
            res = run(clients)
        except CutoverError as e:
            # Permission errors with the first token: retry once with the other token(s) first.
            if len(clients) < 2 or not re.search(r"Authentication error|\b(10000|9109|10001)\b", str(e)):
                raise
            print(f"warn: {e} - retrying with {clients[1].label} first", file=sys.stderr)
            res = run(clients[1:] + clients[:1])
    except CutoverError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        with open(os.path.join(a.out, "error.txt"), "w") as fh:
            fh.write(str(e) + "\n")
        return 1
    with open(os.path.join(a.out, f"{a.action}.json"), "w") as fh:
        json.dump(res, fh, indent=1)
    print(json.dumps(res, indent=1)[:6000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
