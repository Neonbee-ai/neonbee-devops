#!/usr/bin/env node
/**
 * e2e-gate report builder — turns the tier outputs in $GATE_DIR into one
 * Markdown PR comment and sets step outputs. Used by .github/workflows/e2e-gate.yml.
 *
 * Reads (all optional — a missing file means the tier did not run):
 *   t0.exit, t0-findings.json      crawl exit code + tools/exploratory findings
 *   t1.exit, t1.json               "skip" or playwright exit code + JSON report
 *   t2.exit, t2.json               same for journeys
 * Writes:
 *   comment.md                     the PR comment body (without the marker)
 *   failures/                      copies of failure screenshots ONLY
 *   $GITHUB_OUTPUT                 failed, blocking_failed, screenshots
 *
 * Env: GATE_DIR, TIERS, MODULES, SHELL_URL, RUN_URL, GITHUB_OUTPUT.
 * Exported pure helpers are covered by tests/test_e2e_gate_contract.py.
 */
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

/** T0 finding kinds that make the crawl red; the rest are listed as warnings. */
export const T0_HARD = /^(page_error|blank_page|navigation_failed)$|5xx/;

const readJson = (f) => { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch { return null; } };
const readText = (f) => { try { return fs.readFileSync(f, 'utf8').trim(); } catch { return null; } };

/** Flatten a Playwright JSON report into [{file, title, status, screenshots[]}]. */
export function flattenPlaywright(report) {
  const out = [];
  const walk = (suite, file) => {
    const f = suite.file || file;
    for (const spec of suite.specs || []) {
      const results = (spec.tests || []).flatMap((t) => t.results || []);
      const last = results[results.length - 1];
      const skipped = (spec.tests || []).every((t) => t.status === 'skipped');
      const status = skipped ? 'skipped' : spec.ok ? 'passed' : 'failed';
      const screenshots = status === 'failed'
        ? results.flatMap((r) => (r.attachments || []).filter((a) => a.name === 'screenshot' && a.path).map((a) => a.path))
        : [];
      out.push({ file: spec.file || f || '', title: spec.title, status, error: status === 'failed' ? firstLine(last?.error?.message) : '', screenshots });
    }
    for (const s of suite.suites || []) walk(s, f);
  };
  for (const s of report?.suites || []) walk(s, s.file);
  return out;
}

const firstLine = (m) => String(m || '').replace(/\u001b\[[0-9;]*m/g, '').split('\n').find((l) => l.trim()) || '';

/** Tier verdict from its exit marker and parsed results. */
export function verdict(exit, tests) {
  if (exit === null) return 'not run';
  if (exit === 'skip') return 'skipped';
  if (tests && tests.some((t) => t.status === 'failed')) return 'failed';
  if (exit !== '0') return 'failed';
  return 'passed';
}

/** journeys/<module>/<name>.spec.ts → module (T2 grouping). */
export const journeyModule = (file) => (String(file).match(/(?:^|\/)journeys\/([^/]+)\//) || [])[1] || '?';

const ICON = { passed: '✅', failed: '❌', skipped: '⏭️', 'not run': '➖' };

function main() {
  const dir = process.env.GATE_DIR;
  const tiers = (process.env.TIERS || '').split(/\s+/).filter(Boolean);
  const lines = [];
  let blockingFailed = false;
  let anyFailed = false;
  let shots = 0;
  const failDir = path.join(dir, 'failures');
  fs.mkdirSync(failDir, { recursive: true });
  const keepShot = (p, label) => {
    if (!fs.existsSync(p)) return;
    const name = `${String(++shots).padStart(2, '0')}-${label.replace(/[^a-z0-9]+/gi, '-').slice(0, 60)}${path.extname(p)}`;
    fs.copyFileSync(p, path.join(failDir, name));
  };

  lines.push('### 🧪 E2E gate', '');
  lines.push(`Target: ${process.env.SHELL_URL} · modules: \`${process.env.MODULES || 'all'}\` · [run](${process.env.RUN_URL})`, '');
  lines.push('| Tier | Result | Detail |', '|---|---|---|');

  // T0
  const t0exit = tiers.includes('t0') ? readText(path.join(dir, 't0.exit')) : null;
  const f0 = readJson(path.join(dir, 't0-findings.json'));
  const hard = (f0?.findings || []).filter((f) => T0_HARD.test(f.kind));
  const soft = (f0?.findings || []).filter((f) => !T0_HARD.test(f.kind));
  const t0 = t0exit === null ? 'not run' : (t0exit !== '0' || hard.length) ? 'failed' : 'passed';
  if (t0 === 'failed') anyFailed = true;
  lines.push(`| T0 crawl | ${ICON[t0]} ${t0} | ${f0 ? `${f0.routes} routes · ${hard.length} errors · ${soft.length} warnings` : '—'} |`);

  // T1 + T2
  const tier = (key, label) => {
    const exit = tiers.includes(key) ? readText(path.join(dir, `${key}.exit`)) : null;
    const tests = flattenPlaywright(readJson(path.join(dir, `${key}.json`)));
    const v = verdict(exit, tests.length ? tests : null);
    if (v === 'failed') { anyFailed = true; blockingFailed = true; }
    const n = (s) => tests.filter((t) => t.status === s).length;
    const detail = exit === 'skip' ? 'no specs yet' : tests.length ? `${n('passed')} passed · ${n('failed')} failed · ${n('skipped')} skipped` : '—';
    lines.push(`| ${label} | ${ICON[v]} ${v} | ${detail} |`);
    return tests;
  };
  const t1 = tier('t1', 'T1 contracts');
  const t2 = tier('t2', 'T2 journeys');

  if (hard.length) {
    lines.push('', '<details open><summary>T0 crawl errors</summary>', '');
    for (const f of hard.slice(0, 20)) lines.push(`- \`${f.kind}\` ${f.modules?.join(', ') || ''} — ${(f.routes || []).slice(0, 3).join(', ')} (${f.count}) ${firstLine(f.detail ?? f.message).slice(0, 160)}`);
    lines.push('', '</details>');
  }

  const listFailures = (title, tests) => {
    const failed = tests.filter((t) => t.status === 'failed');
    if (!failed.length) return;
    lines.push('', `<details open><summary>${title} failures</summary>`, '');
    for (const t of failed) {
      lines.push(`- ❌ \`${t.file}\` — ${t.title}${t.error ? `: ${t.error.slice(0, 200)}` : ''}`);
      t.screenshots.forEach((p) => keepShot(p, `${t.file}-${t.title}`));
    }
    lines.push('', '</details>');
  };
  listFailures('T1', t1);
  listFailures('T2', t2);

  if (t2.length) {
    const byModule = new Map();
    for (const t of t2) {
      const m = journeyModule(t.file);
      const row = byModule.get(m) || { passed: 0, failed: 0, skipped: 0 };
      row[t.status] = (row[t.status] || 0) + 1;
      byModule.set(m, row);
    }
    lines.push('', '| Module | Journeys passed | failed |', '|---|---|---|');
    for (const [m, r] of byModule) lines.push(`| ${m} | ${r.passed} | ${r.failed ? `❌ ${r.failed}` : 0} |`);
  }

  lines.push('', `_Report-only unless the caller sets \`blocking: true\`. Updated ${new Date().toISOString()}._`);
  fs.writeFileSync(path.join(dir, 'comment.md'), lines.join('\n') + '\n');

  const out = process.env.GITHUB_OUTPUT;
  const kv = `failed=${anyFailed}\nblocking_failed=${blockingFailed}\nscreenshots=${shots}\n`;
  if (out) fs.appendFileSync(out, kv); else process.stdout.write(kv);
  console.log(lines.join('\n'));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
