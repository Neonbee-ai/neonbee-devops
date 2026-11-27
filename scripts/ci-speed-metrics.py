#!/usr/bin/env python3
"""Measure CI speed for pilot repos: queue wait + run time per job type, install-step share.
usage: ci-speed-metrics.py SINCE_ISO OUT.jsonl [repo ...]   (needs gh auth)"""
import json, subprocess, sys, statistics, re
from datetime import datetime

def ts(s): return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
def gh(path):
    return json.loads(subprocess.check_output(["gh", "api", "--paginate", path, "--jq", "."], text=True) or "null") if False else \
        json.loads(subprocess.check_output(["gh", "api", path], text=True))

def classify(name):
    n = name.lower()
    for k in ("route", "security", "notify", "unit", "gate"):
        if k in n: return "unit" if k in ("unit", "gate") else k
    if re.search(r"build|deploy", n): return "build-deploy"
    return "other"

def pct(v, p):
    v = sorted(v)
    return round(v[min(len(v) - 1, int(len(v) * p))], 1) if v else None

def main(since, out, repos):
    rows, per = [], {}
    for repo in repos:
        runs = gh(f"repos/Neonbee-ai/{repo}/actions/runs?per_page=100&created=%3E%3D{since}")["workflow_runs"]
        for r in runs:
            if r["conclusion"] not in ("success", "failure"): continue
            jobs = gh(f"repos/Neonbee-ai/{repo}/actions/runs/{r['id']}/jobs?per_page=100")["jobs"]
            ends = []
            for j in jobs:
                if not j.get("completed_at") or not j.get("started_at") or j.get("conclusion") in ("skipped", "cancelled"): continue
                t = classify(j["name"]); c, s, e = ts(j["created_at"]), ts(j["started_at"]), ts(j["completed_at"])
                run = (e - s).total_seconds(); q = max(0, (s - c).total_seconds())
                inst = sum(max(0, (ts(x["completed_at"]) - ts(x["started_at"])).total_seconds()) for x in j["steps"]
                           if x.get("completed_at") and x.get("conclusion") == "success" and re.search(r"install dep|npm ci|playwright|install (semgrep|gitleaks)", x["name"], re.I))
                d = per.setdefault(t, {"q": [], "run": [], "inst": [], "tot": 0.0, "insts": 0.0})
                d["q"].append(q); d["run"].append(run); d["tot"] += run; d["insts"] += inst
                ends.append(e)
            if ends and r["event"] in ("pull_request", "push"):
                rows.append({"kind": "run", "repo": repo, "wf": r["name"], "event": r["event"], "branch": r["head_branch"],
                             "end_to_end_s": (max(ends) - ts(r["created_at"])).total_seconds()})
    with open(out, "w") as f:
        for t, d in sorted(per.items()):
            f.write(json.dumps({"kind": "job", "type": t, "n": len(d["run"]), "queue_p50": pct(d["q"], .5), "queue_p90": pct(d["q"], .9),
                                "run_p50": pct(d["run"], .5), "run_p90": pct(d["run"], .9),
                                "install_share": round(d["insts"] / d["tot"], 3) if d["tot"] else 0}) + "\n")
        for ev, wfp in (("pull_request", "PR Gate"), ("push", "Deploy")):
            v = [x["end_to_end_s"] for x in rows if x["event"] == ev and (wfp in x["wf"])]
            f.write(json.dumps({"kind": "e2e", "event": ev, "workflow_like": wfp, "n": len(v), "p50_s": pct(v, .5), "p90_s": pct(v, .9)}) + "\n")
main(sys.argv[1], sys.argv[2], sys.argv[3:] or ["so360-connect-fe", "so360-neura-be", "so360-storefront-api", "so360-accounting-fe", "so360-shell-fe"])
