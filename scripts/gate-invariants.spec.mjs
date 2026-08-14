/**
 * Invariants of the shared prod gate (neonbee-deploy-prod.yml).
 *
 * This workflow gates production deploys for every consumer repo, so a
 * regression here is invisible until it takes a deploy down. On 2026-08-02
 * three prod runs failed on three different slots (runner-02/-03/-05) with
 * `##[error]Unit tests failed` and exit 127 — "command not found", because
 * `npm ci` had left an incomplete node_modules and jest was absent. The reason
 * was unreadable because the gate piped npm test's stderr to /dev/null.
 *
 * These specs pin the two fixes so neither can be quietly undone.
 *
 * Run: npm test   (or: node --test scripts/gate-invariants.spec.mjs)
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const PROD = readFileSync(join(ROOT, ".github/workflows/neonbee-deploy-prod.yml"), "utf8");

/** The gate job's text, up to where build-deploy begins. */
function gateSection() {
  const start = PROD.indexOf("  gate:");
  const end = PROD.indexOf("  build-deploy:");
  assert.ok(start > -1 && end > start, "gate / build-deploy jobs not found");
  return PROD.slice(start, end);
}

test("the gate verifies the test runner exists after npm ci", () => {
  const gate = gateSection();
  assert.match(
    gate,
    /node_modules\/\.bin\/\$TEST_BIN/,
    "gate must check that the test binary resolves — an incomplete npm ci otherwise surfaces as exit 127",
  );
  assert.match(gate, /npm cache clean --force/, "gate must purge the npm cache and reinstall when the runner is missing");
});

test("the gate RUNS the test runner, not merely stats it", () => {
  // runner-03 once produced a node_modules where .bin/jest existed but its own
  // dependency did not ("Cannot find module 'jest-util'"), which exits 1 and
  // therefore reads as a real test failure. Only executing it catches that.
  assert.match(
    gateSection(),
    /--version >\/dev\/null/,
    "gate must execute the runner (--version) to prove it can actually start",
  );
});

test("the gate detects the runner from the package's own test script", () => {
  assert.match(gateSection(), /vitest.*jest|jest.*vitest/s, "TEST_BIN detection must handle both jest and vitest repos");
});

test("a still-missing runner fails loudly instead of masquerading as a test failure", () => {
  assert.match(
    gateSection(),
    /still cannot run after a clean reinstall/,
    "gate must fail with a runner-slot diagnosis, not a silent pass or a fake test failure",
  );
});

test("npm test stderr is captured, never discarded", () => {
  const gate = gateSection();
  assert.ok(
    !/npm test -- \$EXTRA_FLAGS 2>\/dev\/null/.test(gate),
    "`2>/dev/null` on npm test swallows `sh: jest: not found` — the exact message needed to diagnose exit 127",
  );
  assert.match(gate, /2>"\$TEST_ERR"/, "npm test stderr must be captured to a file");
});

test("captured stderr is printed on failure (and only on failure)", () => {
  const gate = gateSection();
  assert.match(gate, /tail -50 "\$TEST_ERR"/, "a failing gate must show the captured stderr");
  // Printing it unconditionally would flood every green run with jest's
  // reporter output, which is why it lives inside the failure branches.
  const unconditional = /^\s*tail -50 "\$TEST_ERR"\s*$/m.test(
    gate.replace(/if \[ \$TEST_EXIT[\s\S]*?fi/g, ""),
  );
  assert.ok(!unconditional, "stderr must only be dumped on failure");
});

test("exit 127 is reported as a missing runner, not as a failing test", () => {
  const gate = gateSection();
  assert.match(gate, /TEST_EXIT -eq 127/, "gate must special-case exit 127");
  assert.match(gate, /NOT a test failure/i, "the 127 message must say it is not a test failure");
});

test("the gate still runs on the self-hosted fleet, never a GitHub-hosted runner", () => {
  assert.ok(
    !/runs-on:\s*ubuntu-latest/.test(gateSection()),
    "CI/CD architecture is fixed: all jobs run on the self-hosted Contabo runner",
  );
});

/**
 * A secret that is declared but never WRITTEN is silently absent in production.
 *
 * `secrets: inherit` only makes a secret available to this reusable workflow.
 * If no step references it, it never reaches .env.ci, the deploy is green, and
 * the service starts as though the secret were unset. PEOPLE_INTERNAL_KEY and
 * CORE_INTERNAL_KEY were both set on their repos and deployed green, and both
 * arrived empty on the VM for exactly this reason — with nothing in the run to
 * indicate it.
 *
 * The write list is therefore the real allowlist. This pins that every declared
 * *_INTERNAL_KEY is actually written.
 */
test("every declared internal key is also WRITTEN to .env.ci", () => {
  const declared = [...PROD.matchAll(/^\s{6}([A-Z0-9_]*INTERNAL(?:_SERVICE)?_KEY[A-Z0-9_]*):\s*\{/gm)]
    .map((m) => m[1]);
  assert.ok(declared.length > 0, "expected internal keys in the secrets block");

  const unwritten = declared.filter(
    (k) => !PROD.includes(`echo "${k}=`),
  );
  assert.deepEqual(
    unwritten,
    [],
    `declared but never written to .env.ci — these would be silently absent in production: ${unwritten.join(", ")}`,
  );
});

test("the two keys that were silently dropped are written", () => {
  for (const k of ["PEOPLE_INTERNAL_KEY", "CORE_INTERNAL_KEY"]) {
    assert.ok(
      PROD.includes(`echo "${k}=`),
      `${k} must be written to .env.ci, not merely declared`,
    );
  }
});
