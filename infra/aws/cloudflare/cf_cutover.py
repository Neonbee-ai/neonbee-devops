#!/usr/bin/env python3
"""cf_cutover.py - Cloudflare side of the HA -> AWS prod cut-over (one hostname at a time).

Actions (run by .github/workflows/neonbee-cf-cutover.yml, tokens come from GH secrets, never printed):
  inventory          list zones, SSL mode, every DNS record on the HA IP or the AWS EIPs
  origin-cert        sign a locally made CSR with Cloudflare Origin CA for <zone> + *.<zone>
                     (the private key never leaves the operator's machine; output = public cert)
  dns-switch <host>  HA A record -> the two AWS EIPs (proxied, two A records)
  dns-revert <host>  back to the HA IP

Safety rules (enforced here, covered by tests/test_cf_cutover.py):
  * only hostnames served by the AWS nb-nginx-sites bundle (server_name in infra/aws/nginx/sites)
  * never Dev/preview hosts (dev-*, pr-*, *.skyoffice360.com, tools.neonbee.app) or moonhive.in
  * never deletes a pre-existing record: switch edits the HA record's content and ADDS the second
    EIP record (comment "cf-cutover:added"); revert edits content back to HA and removes only the
    records this tool added
  * refuses anything unexpected (CNAME, AAAA, DNS-only record, unknown IPs) instead of guessing
  * dry_run (default) prints the plan and writes nothing
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
        re.compile(r"moonhive\.in$"), re.compile(r"^tools\.neonbee\.app$"), re.compile(r"preview"))
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


def check_host(host, allowed):
    host = host.strip().lower().rstrip(".")
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
            if z["name"] not in SKIP_ZONES and z["name"] not in seen:
                seen[z["name"]] = (cf, z)
    return seen


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
        for r in cf.list(f"/zones/{z['id']}/dns_records"):
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
    host = check_host(host, allowed)
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
                "header": {"Host": ["api.neonbee.app"]}, "interval": 60, "retries": 2, "timeout": 5,
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
            lbs = cf.list(f"/zones/{z['id']}/load_balancers")
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


def lb_pools(clients, zone_name, dry_run, eips=POOL_EIPS):
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


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["inventory", "origin-cert", "dns-switch", "dns-revert",
                                       "lb-inventory", "lb-pools", "lb-switch", "lb-revert"])
    p.add_argument("--host", default="")
    p.add_argument("--eips", default=",".join(DEFAULT_EIPS))
    p.add_argument("--csr-b64", default=os.environ.get("CSR_B64", ""))
    p.add_argument("--out", default="cf-out")
    p.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    a = p.parse_args(argv)
    clients = [CF(os.environ[n], n) for n in ("CF_SAAS_API_TOKEN", "CF_PREVIEW_API_TOKEN") if os.environ.get(n)]
    if not clients:
        print("no Cloudflare token in env", file=sys.stderr)
        return 2
    eips = tuple(x.strip() for x in a.eips.split(",") if x.strip())
    allowed = bundle_hosts()
    os.makedirs(a.out, exist_ok=True)
    try:
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
            res = lb_pools(clients, a.host or "neonbee.app", not a.apply)
        elif a.action in ("lb-switch", "lb-revert"):
            res = lb_change(clients, a.host, allowed, a.action == "lb-revert", not a.apply)
        else:
            res = dns_change(clients, a.host, eips, allowed, a.action == "dns-revert", not a.apply)
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
