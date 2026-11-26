#!/usr/bin/env node
// Night train: unattended overnight release on the nb-ci controller VM (no Claude, deterministic).
// systemd timer night-train.timer runs one tick every 15 min; after NT_DEADLINE one FINAL summary is written,
// later ticks exit at once (night-train-run.py also skips them), so no more merges or Pulse writes.
//
// Per tick:
//  1. open PRs labelled `night-train` in Neonbee-ai + Moonhive-Pvt-Ltd (base develop)
//  2. skip PRs with label needs-migration, any database/migrations/** or *.sql file, or repo neonbee-devops;
//     merge (--merge --match-head-commit, never --admin/force) only when every check is green and mergeable
//  3. wait for the develop deploy on the merge sha (+ AQA result when the repo has an AQA trigger), then
//     open/reuse ONE develop->main PR per repo, merge when green, wait for the main deploy, flag "Primary VM: failed"
//  4. failures -> Pulse comment on every `Pulse: <id>` trailer task + one "[night-train] <repo> <stage> failed" subtask
//  5. summary each tick on Pulse task "[Night train] 2026-11-27 release" (Neonbee project)
//  6. idempotent state file; a failing PR/head is retried for 2 ticks, then given up
//
// Pulse: PULSE_TOKEN + PULSE_MCP_URL from the env (controller: Secret Manager nbci-pulse-token via night-train-run.py);
// off the controller the old ~/.autoqa/runner.env + tracker.json files are the fallback. gh auth: GH_TOKEN (GitHub App).
// Env: DRY=1 (log only, no merge/PR/Pulse writes) · NT_QUERY="<gh search query>" (dry-run against PRs without
// the label) · NT_DEADLINE (ISO, default 2026-11-27T06:00:00Z) · NT_AQA_REPOS (comma list of repos with an AQA
// trigger; default none) · NT_HOME (default ~/.night-train; controller /var/lib/night-train). Stop file: $NT_HOME/STOP.
// State is per night: $NT_HOME/state-<deadline date>.json. Merges use `gh pr merge --merge --match-head-commit`
// directly (never --admin); a PR merges only when its own GitHub checks are green.
import { execFileSync } from "child_process";
import fs from "fs"; import os from "os"; import path from "path"; import crypto from "crypto";

const DRY = process.env.DRY === "1";
const HOME = process.env.NT_HOME || path.join(os.homedir(), ".night-train");
const DEADLINE = new Date(process.env.NT_DEADLINE || "2026-11-27T06:00:00Z");
const ORGS = ["Neonbee-ai", "Moonhive-Pvt-Ltd"];
const LABEL = "night-train";
const AQA_REPOS = (process.env.NT_AQA_REPOS || "").split(",").map((s) => s.trim()).filter(Boolean);
const PROJECT = "f0df5df0-9685-4d48-9b44-41c609e01ce9"; // Pulse project "Neonbee"
const NIGHT = DEADLINE.toISOString().slice(0, 10);
const SUMMARY_TITLE = `[Night train] ${NIGHT} release`;
const MAX_FAIL_TICKS = 2, DEPLOY_WAIT_MIN = 60, AQA_WAIT_MIN = 30;
const STATE_F = path.join(HOME, `${DRY ? "state.dry" : "state"}-${NIGHT}.json`);
const LOCK = path.join(HOME, "lock");
fs.mkdirSync(HOME, { recursive: true });

const now = () => new Date();
const ts = () => now().toISOString().replace(/\.\d+Z$/, "Z");
const log = (...a) => console.log(`[${ts()}]${DRY ? " DRY" : ""}`, ...a);
const sh = (cmd, args, o = {}) => execFileSync(cmd, args, { encoding: "utf8", maxBuffer: 256 << 20, timeout: o.timeout || 120000, stdio: ["ignore", "pipe", "pipe"] });
const gh = (...args) => sh("gh", args);
const ghj = (...args) => JSON.parse(gh(...args) || "null");
const tryGh = (...args) => { try { return { ok: true, out: gh(...args) }; } catch (e) { return { ok: false, out: String(e.stderr || e.message).slice(0, 600) }; } };
const minsSince = (iso) => (Date.now() - new Date(iso).getTime()) / 60000;
const clean = (s) => s.split("\t").slice(-1)[0].replace(/^\d{4}-\d\d-\d\dT[\d:.]+Z ?/, "").replace(/\x1b\[[0-9;]*m/g, "").trim();

// ---------- state + lock ----------
function loadState() { try { return JSON.parse(fs.readFileSync(STATE_F, "utf8")); } catch { return { prs: {}, mains: {}, reported: {}, summaryId: null, summaryHash: null, ticks: 0 }; } }
function saveState(s) { fs.writeFileSync(STATE_F + ".tmp", JSON.stringify(s, null, 1)); fs.renameSync(STATE_F + ".tmp", STATE_F); }
function lock() {
  try { fs.mkdirSync(LOCK); } catch {
    let pid = 0; try { pid = +fs.readFileSync(path.join(LOCK, "pid"), "utf8"); } catch {}
    try { if (pid) { process.kill(pid, 0); log("previous tick still running, pid", pid); return false; } } catch {}
  }
  fs.writeFileSync(path.join(LOCK, "pid"), String(process.pid)); return true;
}
const unlock = () => { try { fs.rmSync(LOCK, { recursive: true, force: true }); } catch {} };

// ---------- Pulse (MCP over streamable HTTP; token from the env, never logged) ----------
let psess = null, pn = 1, pconf = null;
function pulseConf() {
  if (pconf) return pconf;
  const rd = (f) => { try { return fs.readFileSync(path.join(os.homedir(), f), "utf8"); } catch { return ""; } };
  const env = rd(".autoqa/runner.env").match(/^PULSE_TOKEN=(.*)$/m);
  const tok = process.env.PULSE_TOKEN || (env && env[1].trim().replace(/^["']|["']$/g, ""));
  let url = process.env.PULSE_MCP_URL; if (!url) { try { url = JSON.parse(rd(".autoqa/tracker.json")).mcp.url; } catch {} }
  if (!tok || !url) throw new Error("PULSE_TOKEN / PULSE_MCP_URL not set");
  return (pconf = { url, headers: { Authorization: `Bearer ${tok}` } });
}
async function rpc(method, params, notify) {
  const k = pulseConf(), h = { ...k.headers, "Content-Type": "application/json", Accept: "application/json, text/event-stream" };
  if (psess) h["Mcp-Session-Id"] = psess;
  const r = await fetch(k.url, { method: "POST", headers: h, body: JSON.stringify(notify ? { jsonrpc: "2.0", method, params } : { jsonrpc: "2.0", id: pn++, method, params }), signal: AbortSignal.timeout(60000) });
  const sid = r.headers.get("mcp-session-id"); if (sid) psess = sid; if (notify) return null;
  const t = await r.text(); if (!r.ok) throw new Error(`pulse ${method} HTTP ${r.status}`);
  let m = null;
  if (/^\s*\{/.test(t)) m = JSON.parse(t); else for (const l of t.split("\n")) if (l.startsWith("data:")) { try { const j = JSON.parse(l.slice(5)); if (j.id !== undefined) m = j; } catch {} }
  if (!m || m.error) throw new Error(`pulse ${method}: ${JSON.stringify(m?.error || "empty").slice(0, 200)}`);
  return m.result;
}
async function pulse(name, args, write = true) {
  if (DRY && write) { log(`pulse ${name}`, JSON.stringify(args).slice(0, 300)); return ""; }
  if (!psess) { await rpc("initialize", { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "night-train", version: "1" } }); await rpc("notifications/initialized", {}, true); }
  const r = await rpc("tools/call", { name, arguments: args }), t = (r.content || []).map((x) => x.text || "").join("\n");
  if (r.isError) throw new Error(`pulse ${name}: ${t.slice(0, 200)}`);
  return t;
}
const UUID = /\b[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?\b/; // Pulse returns 8-char ids

async function ensureSummary(S) {
  if (S.summaryId) return S.summaryId;
  const hit = (await pulse("search", { query: SUMMARY_TITLE, max_chars: 4000 }, false)).split("\n").find((l) => l.includes(SUMMARY_TITLE));
  let id = hit?.trim().match(UUID)?.[0];
  if (!id) {
    const t = await pulse("create_task", { project_id: PROJECT, title: SUMMARY_TITLE, priority: "High", tags: ["devops", "release"],
      description: `Unattended overnight release (neonbee-devops runner/controller/night-train.mjs on the nb-ci controller VM). Summary is refreshed every 15 min until ${DEADLINE.toISOString()}.` });
    id = t.match(UUID)?.[0] || (DRY ? "DRY-SUMMARY" : null);
  }
  if (!id) throw new Error("could not create/find summary task");
  if (!DRY) S.summaryId = id;
  return id;
}

// ---------- GitHub helpers ----------
function checksOf(rollup) {
  const items = (rollup || []).map((c) => c.__typename === "StatusContext"
    ? { name: c.context, done: c.state !== "PENDING" && c.state !== "EXPECTED", ok: c.state === "SUCCESS", url: c.targetUrl }
    : { name: c.name || c.workflowName, done: c.status === "COMPLETED", ok: ["SUCCESS", "SKIPPED", "NEUTRAL"].includes(c.conclusion), url: c.detailsUrl, concl: c.conclusion });
  const failed = items.filter((c) => c.done && !c.ok), pending = items.filter((c) => !c.done);
  return { items, failed, pending, green: items.length > 0 && !failed.length && !pending.length };
}
function errorLines(repo, url) {
  const m = url && url.match(/actions\/runs\/(\d+)(?:\/job\/(\d+))?/); if (!m) return "";
  const r = m[2] ? tryGh("run", "view", "-R", repo, "--job", m[2], "--log-failed") : tryGh("run", "view", m[1], "-R", repo, "--log-failed");
  if (!r.ok) return "";
  const lines = r.out.split("\n").filter((l) => !l.includes("\x1b[36;1m")) // drop echoed script source
    .map((l) => l.split("\t").slice(2).join("\t").replace(/^\d{4}-\d\d-\d\dT[\d:.]+Z ?/, "").replace(/\x1b\[[0-9;]*m/g, "").trim()).filter(Boolean);
  const strong = lines.filter((l) => /##\[error\]|^error|Error:|FAIL\b|✕|✗|failed|AssertionError|Expected/i.test(l) && !/::warning::|^shell:/.test(l));
  return (strong.length ? strong : lines.slice(-8)).slice(0, 8).map((l) => l.slice(0, 220)).join("\n");
}
function prFiles(repo, n) {
  return gh("api", "--paginate", `repos/${repo}/pulls/${n}/files?per_page=100`, "-q", ".[].filename").split("\n").filter(Boolean);
}
const isMigration = (f) => /(^|\/)database\/migrations\//.test(f) || /\.sql$/i.test(f);
function pulseIds(commits) {
  const ids = new Set();
  // `Pulse: <id>` trailers and inline "(Pulse ad78c802 c31ad25e)" mentions; ids = 8-hex prefix or full UUID
  for (const c of commits || []) for (const m of `${c.messageHeadline}\n${c.messageBody}`.matchAll(/\bPulse:?[ \t]+((?:[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?\b[ \t,]*)+)/gi))
    for (const id of m[1].split(/[\s,]+/)) if (/^[0-9a-f]{8}/i.test(id)) ids.add(id.toLowerCase());
  return [...ids];
}
function viewPr(repo, n) {
  return ghj("pr", "view", String(n), "-R", repo, "--json",
    "number,url,state,isDraft,baseRefName,headRefName,headRefOid,mergeable,mergeStateStatus,labels,statusCheckRollup,commits,mergeCommit,mergedAt");
}
function deployRuns(repo, branch, sha) {
  const runs = ghj("run", "list", "-R", repo, "--branch", branch, "--event", "push", "--limit", "30", "--json", "databaseId,workflowName,name,headSha,status,conclusion,url,createdAt") || [];
  const isDeploy = (r) => /deploy/i.test(r.workflowName || r.name) && !/preview/i.test(r.workflowName || r.name);
  return { mine: runs.filter((r) => r.headSha === sha && isDeploy(r)), anyDeploy: runs.some(isDeploy) };
}
function hasDeployWorkflow(repo, branch) {
  const wfs = ghj("api", `repos/${repo}/actions/workflows`, "-q", "[.workflows[]|select(.state==\"active\")|.name]") || [];
  const want = branch === "main" ? /deploy.*prod|production/i : /deploy/i;
  return wfs.some((n) => want.test(n) && !/preview/i.test(n) && (branch === "main" || !/prod/i.test(n)));
}
// "green" | "red" | "wait" | "none" (repo has no deploy workflow for that branch)
function deployStatus(repo, branch, sha, since) {
  const { mine } = deployRuns(repo, branch, sha);
  if (!mine.length) {
    if (!hasDeployWorkflow(repo, branch)) return { st: "none" };
    return minsSince(since) > DEPLOY_WAIT_MIN ? { st: "red", why: `no ${branch} deploy run for ${sha.slice(0, 9)} after ${DEPLOY_WAIT_MIN} min` } : { st: "wait" };
  }
  if (mine.some((r) => r.status !== "completed")) return minsSince(since) > 150 ? { st: "red", why: "deploy still running after 150 min" } : { st: "wait" };
  const bad = mine.find((r) => r.conclusion !== "success" && r.conclusion !== "skipped");
  if (bad) return { st: "red", why: `${bad.workflowName}: ${bad.conclusion} ${bad.url}`, err: errorLines(repo, bad.url) };
  if (branch === "main") for (const r of mine) {
    const l = tryGh("run", "view", String(r.databaseId), "-R", repo, "--log");
    if (l.ok && /Primary VM:\s*failed/i.test(l.out)) {
      const ctx = l.out.split("\n").map(clean).filter((x) => /Primary VM|HA|fallback|failed/i.test(x)).slice(0, 6).join("\n");
      return { st: "red", why: `RED FLAG "Primary VM: failed" in ${r.workflowName} ${r.url}`, err: ctx };
    }
  }
  return { st: "green", runs: mine.map((r) => r.url) };
}
async function aqaStatus(repo, ids, since) {
  if (!AQA_REPOS.includes(repo.split("/")[1])) return { st: "none" };
  for (const id of ids) {
    let t = ""; try { t = await pulse("get_task_comments", { task_id: id, since }, false); } catch {}
    if (/(AQA|AutoQA)[^\n]*(fail|red)/i.test(t)) return { st: "red", why: `AQA failed on Pulse ${id}`, err: (t.match(/[^\n]*(AQA|AutoQA)[^\n]*/i) || [""])[0].slice(0, 300) };
    if (/(AQA|AutoQA)[^\n]*(pass|green)/i.test(t)) return { st: "green" };
  }
  return minsSince(since) > AQA_WAIT_MIN ? { st: "red", why: `no AQA result on the Pulse task within ${AQA_WAIT_MIN} min` } : { st: "wait" };
}

// ---------- failure reporting (once per key+stage+sha) ----------
async function report(S, { repo, prUrl, stage, sha, why, err, ids }) {
  const k = crypto.createHash("sha1").update(`${repo}|${stage}|${sha}`).digest("hex").slice(0, 16);
  if (S.reported[k]) return;
  const short = repo.split("/")[1];
  const body = `[night-train] ${short} — ${stage} failed\nRepo: ${repo}\nPR: ${prUrl}\nStage: ${stage}\nHead/merge sha: ${sha}\nFailing: ${why}\n${err ? `First error lines:\n${err}\n` : ""}Night train stopped retrying this item; fix it and re-label/re-run in the morning.`;
  log("FAIL", repo, stage, why, err ? `\n  ${err.split("\n").join("\n  ")}` : "");
  const summary = await ensureSummary(S);
  for (const id of ids.slice(0, 25)) { try { await pulse("add_comment", { task_id: id, message: body }); } catch (e) { log("pulse comment failed", id, e.message); } }
  const parent = ids.length === 1 ? ids[0] : summary;
  try { await pulse("create_task", { project_id: PROJECT, parent_task_id: parent, title: `[night-train] ${short} ${stage} failed`, description: body, priority: "High", tags: ["release"] }); }
  catch (e) { log("pulse subtask failed", e.message); if (parent !== summary) try { await pulse("create_task", { project_id: PROJECT, parent_task_id: summary, title: `[night-train] ${short} ${stage} failed`, description: body, priority: "High", tags: ["release"] }); } catch {} }
  if (!DRY) S.reported[k] = ts();
}
// count a failing tick; returns true once the PR/head has failed MAX_FAIL_TICKS ticks (give up)
function failTick(rec, sha, why) {
  if (rec.failSha !== sha) { rec.failSha = sha; rec.failTicks = 0; }
  rec.failTicks = (rec.failTicks || 0) + 1; rec.why = why;
  return rec.failTicks >= MAX_FAIL_TICKS;
}

// ---------- develop stage ----------
async function stepDevelop(S, key, rec) {
  const [repo, n] = key.split("#");
  if (["merged-develop", "deploy-develop", "done-develop", "failed", "skipped"].includes(rec.stage)) return;
  let pr; try { pr = viewPr(repo, n); } catch (e) { rec.stage = "waiting"; rec.why = "cannot read PR"; return; }
  rec.url = pr.url; rec.ids = pulseIds(pr.commits);
  if (pr.state === "MERGED") { rec.stage = "deploy-develop"; rec.mergeSha = pr.mergeCommit?.oid; rec.mergedAt = pr.mergedAt; rec.why = "merged outside the train"; return; }
  if (pr.state !== "OPEN") { rec.stage = "skipped"; rec.why = `PR ${pr.state}`; return; }
  if (pr.baseRefName !== "develop") { rec.stage = "skipped"; rec.why = `base ${pr.baseRefName}, not develop`; return; }
  const labels = (pr.labels || []).map((l) => l.name);
  if (repo.endsWith("/neonbee-devops")) { rec.stage = "skipped"; rec.skip = "migration"; rec.why = "neonbee-devops (manual)"; return; }
  if (labels.includes("needs-migration")) { rec.stage = "skipped"; rec.skip = "migration"; rec.why = "label needs-migration"; return; }
  const mig = prFiles(repo, n).filter(isMigration);
  if (mig.length) { rec.stage = "skipped"; rec.skip = "migration"; rec.why = `migration/sql files: ${mig.slice(0, 3).join(", ")}${mig.length > 3 ? ` +${mig.length - 3}` : ""}`; return; }
  if (pr.isDraft) { rec.stage = "waiting"; rec.why = "draft"; return; }
  const c = checksOf(pr.statusCheckRollup), sha = pr.headRefOid;
  if (c.pending.length) { rec.stage = "waiting"; rec.why = `checks pending: ${c.pending.map((x) => x.name).slice(0, 4).join(", ")}`; return; }
  let why = null, err = "";
  if (!c.items.length) why = "no checks reported on the PR";
  else if (c.failed.length) { why = `failing checks: ${[...new Set(c.failed.map((x) => `${x.name} (${x.concl || "failure"})`))].join(", ")}`; err = errorLines(repo, c.failed[0].url); }
  else if (pr.mergeable === "UNKNOWN") { rec.stage = "waiting"; rec.why = "mergeability computing"; return; }
  else if (pr.mergeable !== "MERGEABLE" || !["CLEAN", "HAS_HOOKS"].includes(pr.mergeStateStatus)) why = `not mergeable: ${pr.mergeable}/${pr.mergeStateStatus}`;
  if (why) {
    if (failTick(rec, sha, why)) { rec.stage = "failed"; rec.failStage = "develop CI"; await report(S, { repo, prUrl: pr.url, stage: "develop CI", sha, why, err, ids: rec.ids }); }
    else rec.stage = "retry";
    return;
  }
  log("MERGE develop", key, sha.slice(0, 9));
  if (DRY) { rec.stage = "would-merge-develop"; rec.why = `green, mergeable @${sha.slice(0, 9)}`; return; }
  const m = tryGh("pr", "merge", n, "-R", repo, "--merge", "--match-head-commit", sha);
  if (!m.ok) {
    if (failTick(rec, sha, `merge refused: ${m.out.split("\n")[0]}`)) { rec.stage = "failed"; rec.failStage = "develop merge"; await report(S, { repo, prUrl: pr.url, stage: "develop merge", sha, why: rec.why, err: m.out, ids: rec.ids }); }
    else rec.stage = "retry";
    return;
  }
  const after = viewPr(repo, n);
  rec.stage = "deploy-develop"; rec.mergeSha = after.mergeCommit?.oid; rec.mergedAt = after.mergedAt || ts(); rec.why = "merged; waiting for develop deploy";
}
async function stepDevelopDeploy(S, key, rec) {
  if (rec.stage !== "deploy-develop" || !rec.mergeSha) return;
  const repo = key.split("#")[0];
  if (!rec.deployOk) {
    const d = deployStatus(repo, "develop", rec.mergeSha, rec.mergedAt);
    if (d.st === "wait") { rec.why = "develop deploy running"; return; }
    if (d.st === "red") { rec.stage = "failed"; rec.failStage = "develop deploy"; rec.why = d.why; await report(S, { repo, prUrl: rec.url, stage: "develop deploy", sha: rec.mergeSha, why: d.why, err: d.err, ids: rec.ids }); return; }
    rec.deployOk = d.st; rec.deployAt = ts();
  }
  const a = await aqaStatus(repo, rec.ids || [], rec.deployAt);
  if (a.st === "wait") { rec.why = "waiting for AQA"; return; }
  if (a.st === "red") { rec.stage = "failed"; rec.failStage = "develop AQA"; rec.why = a.why; await report(S, { repo, prUrl: rec.url, stage: "develop AQA", sha: rec.mergeSha, why: a.why, err: a.err, ids: rec.ids }); return; }
  rec.stage = "done-develop"; rec.why = `develop deploy ${rec.deployOk}${a.st === "none" ? ", no AQA trigger" : ", AQA green"}`;
}

// ---------- main stage (one develop->main PR per repo) ----------
async function stepMain(S, repo, recs) {
  const M = (S.mains[repo] ||= { stage: "pending" });
  if (["merged-main", "failed", "skipped"].includes(M.stage)) return;
  if (recs.some((r) => ["deploy-develop", "waiting", "retry", "new"].includes(r.stage))) { M.why = "develop still in flight"; return; }
  if (!recs.some((r) => r.stage === "done-develop")) return;
  const ids = [...new Set(recs.flatMap((r) => r.ids || []))];
  if (M.stage === "deploy-main") {
    const d = deployStatus(repo, "main", M.mergeSha, M.mergedAt);
    if (d.st === "wait") { M.why = "main deploy running"; return; }
    if (d.st === "red") { M.stage = "failed"; M.failStage = "main deploy"; M.why = d.why; await report(S, { repo, prUrl: M.url, stage: "main deploy", sha: M.mergeSha, why: d.why, err: d.err, ids }); return; }
    M.stage = "merged-main"; M.why = `main deploy ${d.st}`; return;
  }
  let pr = (ghj("pr", "list", "-R", repo, "--base", "main", "--head", "develop", "--state", "open", "--json", "number") || [])[0];
  if (!pr) {
    const cmp = ghj("api", `repos/${repo}/compare/main...develop`, "-q", "{ahead:.ahead_by}");
    if (!cmp?.ahead) { M.stage = "merged-main"; M.why = "main already contains develop"; return; }
    log("CREATE PR develop->main", repo);
    if (DRY) { M.stage = "would-open-main-pr"; return; }
    const body = `Night train ${ts().slice(0, 10)}: promote develop to main after green develop deploy.\n\nRiding PRs:\n${recs.filter((r) => r.stage === "done-develop").map((r) => `- ${r.url}`).join("\n")}\n\nPulse: ${ids.length ? ids.join(", ") : "none"}`;
    const c = tryGh("pr", "create", "-R", repo, "--base", "main", "--head", "develop", "--title", `release(night-train ${ts().slice(0, 10)}): develop → main`, "--body", body);
    if (!c.ok) { M.stage = "failed"; M.failStage = "main PR"; M.why = `cannot open develop→main PR: ${c.out.split("\n")[0]}`; await report(S, { repo, prUrl: "-", stage: "main PR", sha: "develop", why: M.why, err: c.out, ids }); return; }
    pr = { number: +(c.out.match(/pull\/(\d+)/) || [])[1] };
    M.why = "develop→main PR opened"; M.stage = "main-pr"; M.number = pr.number; return; // let checks start
  }
  M.number = pr.number;
  const p = viewPr(repo, pr.number); M.url = p.url;
  const mig = prFiles(repo, pr.number).filter(isMigration);
  if (mig.length) { M.stage = "skipped"; M.skip = "migration"; M.why = `develop→main carries migration/sql: ${mig.slice(0, 3).join(", ")}`; return; }
  const c = checksOf(p.statusCheckRollup), sha = p.headRefOid;
  if (c.pending.length || p.mergeable === "UNKNOWN") { M.stage = "main-pr"; M.why = "main PR checks pending"; return; }
  let why = null, err = "";
  if (!c.items.length) why = "no checks on develop→main PR";
  else if (c.failed.length) { why = `failing checks: ${[...new Set(c.failed.map((x) => x.name))].join(", ")}`; err = errorLines(repo, c.failed[0].url); }
  else if (p.mergeable !== "MERGEABLE" || !["CLEAN", "HAS_HOOKS"].includes(p.mergeStateStatus)) why = `not mergeable: ${p.mergeable}/${p.mergeStateStatus}`;
  if (why) {
    if (failTick(M, sha, why)) { M.stage = "failed"; M.failStage = "main CI"; await report(S, { repo, prUrl: p.url, stage: "main CI", sha, why, err, ids }); }
    else M.stage = "main-pr";
    return;
  }
  log("MERGE main", repo, `#${pr.number}`, sha.slice(0, 9));
  if (DRY) { M.stage = "would-merge-main"; return; }
  const m = tryGh("pr", "merge", String(pr.number), "-R", repo, "--merge", "--match-head-commit", sha);
  if (!m.ok) {
    if (failTick(M, sha, `merge refused: ${m.out.split("\n")[0]}`)) { M.stage = "failed"; M.failStage = "main merge"; await report(S, { repo, prUrl: p.url, stage: "main merge", sha, why: M.why, err: m.out, ids }); }
    return;
  }
  const after = viewPr(repo, pr.number);
  M.stage = "deploy-main"; M.mergeSha = after.mergeCommit?.oid; M.mergedAt = after.mergedAt || ts(); M.why = "merged; waiting for main deploy";
}

// ---------- summary ----------
async function writeSummary(S, final) {
  const P = Object.entries(S.prs), Mn = Object.entries(S.mains);
  const row = (k, r) => `- ${k.replace(/^[^/]+\//, "")} — ${r.stage}${r.why ? `: ${r.why}` : ""}`;
  const g = {
    "Merged develop": P.filter(([, r]) => ["deploy-develop", "done-develop"].includes(r.stage) || (r.stage === "failed" && /deploy|AQA/.test(r.failStage || ""))),
    "Merged main": Mn.filter(([, m]) => ["merged-main", "deploy-main"].includes(m.stage)).map(([k, m]) => [`${k} main#${m.number || "-"}`, m]),
    "Skipped (migration/manual)": [...P.filter(([, r]) => r.stage === "skipped"), ...Mn.filter(([, m]) => m.stage === "skipped").map(([k, m]) => [`${k} main`, m])],
    Failed: [...P.filter(([, r]) => r.stage === "failed"), ...Mn.filter(([, m]) => m.stage === "failed").map(([k, m]) => [`${k} main`, m])],
    Waiting: [...P.filter(([, r]) => ["new", "waiting", "retry", "would-merge-develop"].includes(r.stage)), ...Mn.filter(([, m]) => ["pending", "main-pr", "would-open-main-pr", "would-merge-main"].includes(m.stage) && m.why).map(([k, m]) => [`${k} main`, m])],
  };
  const body = Object.entries(g).map(([h, xs]) => `${h} (${xs.length})\n${xs.map(([k, r]) => row(k, r)).join("\n") || "- none"}`).join("\n\n");
  const hash = crypto.createHash("sha1").update(body).digest("hex");
  console.log(body);
  const id = await ensureSummary(S);
  const head = `${final ? "FINAL — train stopped at deadline" : "Live"} · tick ${S.ticks} · ${ts()} · deadline ${DEADLINE.toISOString()}`;
  await pulse("update_task", { task_id: id, description: `Night train (neonbee-devops runner/controller/night-train.mjs, nb-ci controller VM). ${head}\nLabel PRs \`night-train\` to board.\n\n${body}` });
  if (hash !== S.summaryHash || final) { await pulse("add_comment", { task_id: id, message: `${head}\n\n${body}` }); if (!DRY) S.summaryHash = hash; }
}

// ---------- tick ----------
async function main() {
  if (!lock()) return;
  const S = loadState();
  try {
    if (S.finalAt) { log(`train for ${NIGHT} already stopped at ${S.finalAt}; nothing to do`); return; }
    const final = now() >= DEADLINE || fs.existsSync(path.join(HOME, "STOP"));
    S.ticks++;
    if (!final) {
      const found = [];
      for (const o of ORGS) {
        const q = process.env.NT_QUERY ? [process.env.NT_QUERY] : [];
        const args = ["search", "prs", ...q, "--owner", o, "--state", "open", ...(q.length ? [] : ["--label", LABEL]), "--limit", "100", "--json", "repository,number"];
        const r = tryGh(...args);
        if (!r.ok) { log("search failed", o, r.out.split("\n")[0]); continue; }
        for (const p of JSON.parse(r.out)) found.push(`${p.repository.nameWithOwner}#${p.number}`);
      }
      for (const k of found) S.prs[k] ||= { stage: "new", since: ts() };
      log(`tick ${S.ticks}: ${found.length} labelled open PRs, ${Object.keys(S.prs).length} tracked`);
      for (const [k, r] of Object.entries(S.prs)) {
        try { if (r.stage === "retry" || r.stage === "new" || r.stage === "waiting" || r.stage === "would-merge-develop") await stepDevelop(S, k, r); await stepDevelopDeploy(S, k, r); }
        catch (e) { log("error", k, e.message.slice(0, 300)); r.why = `tick error: ${e.message.slice(0, 120)}`; }
        saveState(S);
      }
      const byRepo = {};
      for (const [k, r] of Object.entries(S.prs)) (byRepo[k.split("#")[0]] ||= []).push(r);
      for (const [repo, recs] of Object.entries(byRepo)) {
        try { await stepMain(S, repo, recs); } catch (e) { log("main error", repo, e.message.slice(0, 300)); }
        saveState(S);
      }
    }
    try { await writeSummary(S, final); } catch (e) { log("summary failed", e.message.slice(0, 200)); }
    saveState(S);
    if (final && !DRY) { S.finalAt = ts(); saveState(S); log("deadline reached — train stopped; later ticks are no-ops"); }
  } finally { unlock(); }
}
main().catch((e) => { log("fatal", e.stack || e.message); unlock(); process.exit(1); });
