/**
 * Behaviour specs for the internal service-key drift audit.
 *
 * This script decides which production secrets get written, so a bug in it is
 * either a silent no-op or an actively wrong value propagated across repos.
 * Both have already happened during its development:
 *
 *   1. `ssh` without -n consumed the loop's STDIN and swallowed the piped drift
 *      list, so a --fix run reported success after repairing exactly ONE repo.
 *   2. Directories carrying a stray src/ that are not repos were reported as
 *      phantom drift.
 *   3. Taking the FIRST value found in production is a coin flip:
 *      NEURA_INTERNAL_KEY has seven services on one value and one straggler on
 *      a shorter legacy key. Propagating the straggler would authenticate a new
 *      caller against nothing — a silent 401 loop indistinguishable from the
 *      drift this tool exists to remove.
 *
 * These pin all three, plus the fallback rule, against the script's source.
 * The script is bash that talks to GitHub and a production VM, so its logic is
 * asserted here rather than executed.
 *
 * Run: npm test
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const script = readFileSync(join(here, "audit-internal-service-keys.sh"), "utf8");

test("Given ssh runs inside a loop / Then -n is passed so it cannot eat the loop's STDIN", () => {
  // Only real invocations — a comment that merely mentions ssh is not a call,
  // and matching those made this spec fail on its own prose.
  const sshCalls = script
    .split("\n")
    .filter((l) => !l.trim().startsWith("#"))
    .filter((l) => /(^|[|;&(\s])ssh\s/.test(l));
  assert.ok(sshCalls.length > 0, "expected the script to invoke ssh");
  for (const call of sshCalls) {
    assert.match(
      call,
      /ssh -n /,
      `ssh must be invoked with -n or it swallows the drift list: ${call}`,
    );
  }
});

test("Given a key with several values in production / Then the script refuses rather than guessing", () => {
  assert.match(
    script,
    /count_variants/,
    "expected a variant count before propagating",
  );
  assert.match(
    script,
    /Refusing to guess/i,
    "expected an explicit refusal when production disagrees",
  );
  // The refusal must actually skip the write, not merely warn beside it.
  assert.match(
    script,
    /__AMBIGUOUS__/,
    "expected ambiguous keys to be marked and skipped",
  );
});

test("Given the canonical value is chosen / Then it is the MAJORITY, not the first match", () => {
  const reader = script.slice(
    script.indexOf("read_canonical()"),
    script.indexOf("count_variants()"),
  );
  assert.match(
    reader,
    /uniq -c/,
    "majority selection requires counting duplicates",
  );
  assert.match(
    reader,
    /sort -rn/,
    "the most frequent value must sort first",
  );
  assert.doesNotMatch(
    reader,
    /\|\s*head -1\s*\|\s*cut -d= -f2-/,
    "must not fall back to naive first-match extraction",
  );
});

test("Given a directory is not a real repo / Then it is filtered before being reported as drift", () => {
  assert.match(
    script,
    /gh repo view/,
    "repo existence must be confirmed with GitHub, not inferred from the filesystem",
  );
  assert.match(
    script,
    /REPO_EXISTS/,
    "expected non-repos to be excluded from the drift set",
  );
});

test("Given a key is only ever an `a || b` fallback / Then its absence is not reported as drift", () => {
  assert.match(script, /fallbacks=/, "expected fallback detection");
  assert.match(
    script,
    /grep -qx "\$key" <<<"\$fallbacks" && continue/,
    "detected fallbacks must be skipped, not merely collected",
  );
});

test("Given --fix is not passed / Then the script never writes a secret", () => {
  const reportOnlyExit = script.indexOf("Re-run with --fix");
  const firstWrite = script.indexOf("gh secret set");
  assert.ok(reportOnlyExit > 0 && firstWrite > 0);
  assert.ok(
    reportOnlyExit < firstWrite,
    "the report-only early exit must come before any secret write",
  );
});

test("Given drift exists / Then the exit code is non-zero so CI can gate on it", () => {
  assert.match(
    script,
    /exit 1/,
    "drift must fail the process for this to be usable as a check",
  );
});

test("Given values are read / Then they come from production, never a local .env", () => {
  assert.match(script, /PROD_VM=/, "canonical values must come from the VM");
  assert.doesNotMatch(
    script,
    /\$ROOT\/[^\n]*\.env/,
    "a local .env holds DEV values; setting one as a production secret is indistinguishable from a bogus key",
  );
});

test("Given zero drift / Then the success path does not crash on an empty array", () => {
  // `set -u` plus an array declared-but-never-assigned makes "${DRIFT[@]}" an
  // unbound-variable error, so the script failed exactly when everything was
  // FINE — the one outcome nobody would think to test.
  assert.match(
    script,
    /declare -a DRIFT=\(\)/,
    "DRIFT must be initialised empty, not merely declared",
  );
  assert.doesNotMatch(
    script,
    /\$\{#DRIFT\[@\]:-/,
    "${#arr[@]:-0} is not valid substitution syntax",
  );
});
