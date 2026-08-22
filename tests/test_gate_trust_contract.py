"""
BDD specs — the gate must fail loudly, or it is not a gate.

Background
----------
Every deploy in the estate passes through a `gate` job before anything is built
or rsynced. The value of that gate is entirely in its ability to say *no*. Four
defects were found on 2026-08-19 that each let a broken or unverified build
through while reporting green:

1. `neonbee-deploy-prod.yml` wrapped the unit suite in `timeout 120` and then
   reset the exit code to 0 on expiry — a hung suite deployed to production with
   nothing but a warning annotation.

2. That timeout was guarded by `command -v timeout`, which is absent on macOS.
   The Mac Mini slots took the `contabo` label on 2026-08-19, so the slots now
   serving deploys had no hang protection at all.

3. The gate captured test stderr and printed it only on failure. jest and vitest
   write their summary to stderr, so a passing gate printed no evidence: a run of
   2,661 tests and a run of 0 tests were indistinguishable in the log. Combined
   with `--passWithNoTests`, an empty suite was an invisible pass.

4. The API-contract and integration tiers existed only in
   `neonbee-deploy-backend.yml` (the develop path). The estate pushes `main`,
   which routes to `neonbee-deploy-prod.yml` — so those specs were written,
   maintained, and never once enforced on a deploy that reached production.

These specs lock out all four. Run: python3 -m unittest discover tests
"""

import os
import re
import unittest

WORKFLOW_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".github", "workflows"
)

# The reusable workflows that run a test gate.
GATE_WORKFLOWS = [
    "neonbee-deploy-backend.yml",
    "neonbee-deploy-vite-mfe.yml",
    "neonbee-deploy-nextjs.yml",
    "neonbee-deploy-prod.yml",
    "neonbee-pr-gate.yml",
]

PROD = "neonbee-deploy-prod.yml"
PR_GATE = "neonbee-pr-gate.yml"


def read_workflow(name):
    with open(os.path.join(WORKFLOW_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


def strip_comments(body):
    """Drop `#` comment lines so prose describing a defect is not mistaken for it."""
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )


class GivenATestSuiteThatNeverFinishes(unittest.TestCase):
    """A hung suite is a failure, never a pass."""

    # `RC=0` / `TEST_RC=0` appearing on the same line as a 124/142 timeout check.
    TIMEOUT_RESET = re.compile(
        r"(?:124|142).*?(?:RC|TEST_RC|TEST_EXIT)\s*=\s*0"
        r"|(?:RC|TEST_RC|TEST_EXIT)\s*=\s*0.*?(?:124|142)"
    )

    def test_when_the_suite_times_out_then_no_workflow_resets_the_exit_code(self):
        for name in GATE_WORKFLOWS:
            body = strip_comments(read_workflow(name))
            match = self.TIMEOUT_RESET.search(body)
            self.assertIsNone(
                match,
                f"{name}: a timeout exit code is being reset to 0 "
                f"({match.group(0).strip() if match else ''!r}). A suite that "
                "never finishes has verified nothing — treat the timeout as a "
                "failure and raise the ceiling if the suite is legitimately slow.",
            )

    def test_when_the_suite_times_out_then_the_gate_reports_an_error(self):
        body = read_workflow(PROD)
        self.assertIn(
            "::error::Unit tests exceeded",
            body,
            f"{PROD}: a timed-out suite must emit an ::error:: annotation, not a "
            "::warning::. A warning does not fail the job.",
        )


class GivenARunnerWithoutCoreutils(unittest.TestCase):
    """Hang protection must survive on macOS slots, where `timeout` is absent."""

    def test_when_timeout_is_missing_then_a_fallback_is_used(self):
        body = read_workflow(PROD)
        self.assertIn(
            "gtimeout",
            body,
            f"{PROD}: `timeout` is coreutils and does not exist on the Mac Mini "
            "slots that now carry the `contabo` label. Resolve `gtimeout` too.",
        )
        self.assertIn(
            "alarm shift",
            body,
            f"{PROD}: neither `timeout` nor `gtimeout` is present on a stock "
            "macOS runner. A perl `alarm` fallback keeps the ceiling enforced "
            "everywhere; without it the bound silently disappears.",
        )

    def test_when_perl_enforces_the_timeout_then_its_signal_code_is_treated_as_a_timeout(self):
        body = read_workflow(PROD)
        self.assertIn(
            "142",
            body,
            f"{PROD}: perl's SIGALRM exits 128+14=142, not 124. Checking only "
            "124 would classify a perl-enforced timeout as an ordinary failure "
            "and report a misleading cause.",
        )


class GivenAGateThatPasses(unittest.TestCase):
    """A green gate must prove what it ran."""

    def test_when_the_suite_passes_then_the_summary_is_still_printed(self):
        body = read_workflow(PROD)
        self.assertIn(
            "test summary",
            body,
            f"{PROD}: jest and vitest write their summary to stderr, which this "
            "gate captures. Without printing it on success, '2,661 tests passed' "
            "and '0 tests ran' produce identical logs.",
        )

    def test_when_pass_with_no_tests_is_used_then_a_spec_floor_is_asserted(self):
        for name in GATE_WORKFLOWS:
            body = read_workflow(name)
            if "--passWithNoTests" not in strip_comments(body):
                continue
            self.assertIn(
                "SPEC_COUNT",
                body,
                f"{name}: `--passWithNoTests` makes an empty or mis-globbed suite "
                "exit 0. Assert a spec floor before running so a repo with no "
                "tests fails by name instead of reading as a pass.",
            )


class GivenAWarmRunnerSlot(unittest.TestCase):
    """A reused node_modules must never be trusted by a gate."""

    def test_when_a_gate_installs_dependencies_then_it_purges_node_modules_first(self):
        for name in GATE_WORKFLOWS:
            body = strip_comments(read_workflow(name))
            gate = body.split("build-deploy:")[0]
            if "npm ci" not in gate:
                continue
            # Assert ORDER, not mere presence. Two weaker forms of this spec
            # both passed against a broken gate during a mutation run:
            #   - `"rm -rf node_modules" in gate` also matches
            #     `rm -rf node_modules/@so360/*` (the ci-stubs scope purge)
            #   - even an end-of-line match is satisfied by the `runner_ok`
            #     recovery branch, which purges only AFTER a bad install
            # What actually matters is that the FIRST install in the gate is
            # preceded by a whole-tree purge.
            lines = [ln.strip() for ln in gate.splitlines()]
            first_ci = next(i for i, ln in enumerate(lines) if ln.startswith("npm ci"))
            preceding = [ln for ln in lines[:first_ci] if ln]
            self.assertTrue(
                preceding and preceding[-1] == "rm -rf node_modules",
                f"{name}: the first `npm ci` in the gate is not immediately "
                f"preceded by a whole-tree purge (found {preceding[-1]!r}). "
                "Contabo and Mac Mini slots are warm and keep node_modules "
                "between runs, so a stale or partial install is silently reused.",
            )

    def test_when_a_gate_installs_dependencies_then_it_verifies_the_test_runner_runs(self):
        for name in GATE_WORKFLOWS:
            body = read_workflow(name)
            gate = body.split("build-deploy:")[0]
            if "npm ci" not in strip_comments(gate):
                continue
            self.assertIn(
                "runner_ok",
                gate,
                f"{name}: `npm ci` can exit 0 on a corrupted npm cache and still "
                "leave the runner unusable — a missing binary exits 127, a "
                "half-installed one exits 1, and both read as 'tests failed'. "
                "Execute the runner to prove it works.",
            )


class GivenAProductionDeploy(unittest.TestCase):
    """The contract and integration tiers must gate the path that actually ships."""

    def test_when_a_backend_deploys_to_prod_then_contract_tests_run(self):
        body = read_workflow(PROD)
        self.assertIn(
            "contract-tests:",
            body,
            f"{PROD}: the API contract tier exists only in the develop-path "
            "workflow. The estate pushes `main`, which routes here, so those "
            "specs never gate a deploy that reaches production.",
        )

    def test_when_a_backend_deploys_to_prod_then_integration_tests_run(self):
        body = read_workflow(PROD)
        self.assertIn(
            "integration-tests:",
            body,
            f"{PROD}: the integration tier — the only one that catches schema "
            "drift, tenant-scoping regressions and missing service URLs — does "
            "not run on the production path.",
        )

    def test_when_the_tiers_are_defined_then_the_build_waits_on_them(self):
        body = read_workflow(PROD)
        needs = re.search(r"build-deploy:\s*\n\s*needs:\s*\[([^\]]*)\]", body)
        self.assertIsNotNone(needs, f"{PROD}: build-deploy declares no `needs`.")
        declared = needs.group(1)
        for job in ("gate", "contract-tests", "integration-tests"):
            self.assertIn(
                job,
                declared,
                f"{PROD}: build-deploy does not wait on `{job}`, so that tier "
                "cannot block a deploy no matter what it reports.",
            )

    def test_when_a_tier_is_skipped_then_the_build_still_proceeds(self):
        # contract/integration are backend-only. A skipped `needs` skips the
        # dependent job by default, which would strand every MFE and Next.js
        # deploy — the result must be matched explicitly instead.
        body = read_workflow(PROD)
        self.assertIn(
            "needs.contract-tests.result != 'failure'",
            body,
            f"{PROD}: build-deploy must tolerate a *skipped* contract-tests job "
            "(vite-mfe and nextjs deploys skip it) while still refusing a "
            "failed one. A bare `needs` would skip the deploy entirely.",
        )

    def test_when_a_tier_fails_then_the_build_is_blocked(self):
        body = read_workflow(PROD)
        self.assertIn(
            "needs.gate.result == 'success'",
            body,
            f"{PROD}: build-deploy must require the gate to have *succeeded*, "
            "not merely to have finished. `!cancelled()` alone would let a "
            "failed gate through.",
        )


class GivenARepoWithNoTestsForATier(unittest.TestCase):
    """A missing tier is reported by name, never silently skipped or hard-crashed."""

    def test_when_the_script_is_absent_then_the_job_warns_instead_of_crashing(self):
        for name in (PROD, "neonbee-deploy-backend.yml"):
            body = read_workflow(name)
            self.assertIn(
                "NOT enforced",
                body,
                f"{name}: several backends have no `test:contract` script at "
                "all, so `npm run test:contract` dies with `Missing script`. "
                "Introducing a gate must not brick those deploys — warn by "
                "service name so the gap is visible rather than silent.",
            )


class GivenAPullRequestGate(unittest.TestCase):
    """A PR runs against an untrusted ref — it must not be able to deploy."""

    # Any step that moves artefacts onto a VM or restarts a service there.
    DEPLOY_CAPABILITY = re.compile(
        r"\brsync\b|\bscp\b|\bssh-agent\b|\bpm2\b|ssh\s+-o|DIGITALOCEAN_SSH_KEY|CONTABO_SSH_KEY",
        re.IGNORECASE,
    )

    def test_when_a_pull_request_is_gated_then_no_deploy_job_exists(self):
        body = read_workflow(PR_GATE)
        for job in ("build-deploy:", "build:", "deploy:"):
            self.assertNotIn(
                f"\n  {job}",
                body,
                f"{PR_GATE}: defines a `{job}` job. The PR gate must verify "
                "only. Guarding a deploy job with an `if:` leaves production "
                "one bad condition away from a fork's code — the capability "
                "must be absent, not disabled.",
            )

    def test_when_a_pull_request_is_gated_then_it_cannot_reach_a_vm(self):
        body = strip_comments(read_workflow(PR_GATE))
        match = self.DEPLOY_CAPABILITY.search(body)
        self.assertIsNone(
            match,
            f"{PR_GATE}: contains {match.group(0) if match else ''!r}, which can "
            "move code onto or restart a service on a VM. A PR gate runs "
            "untrusted code and must have no such capability at all.",
        )

    def test_when_a_pull_request_is_gated_then_it_still_runs_every_tier(self):
        body = read_workflow(PR_GATE)
        for job in ("contract-tests:", "integration-tests:"):
            self.assertIn(
                job,
                body,
                f"{PR_GATE}: missing `{job}`. Moving verification earlier is "
                "the entire point — a PR gate weaker than the deploy gate just "
                "relocates the problem.",
            )

    def test_when_a_pull_request_is_gated_then_secrets_are_not_required(self):
        # A required secret that a fork PR cannot supply fails the gate for
        # reasons unrelated to the change under review.
        body = read_workflow(PR_GATE)
        secrets_block = body.split("secrets:")[1].split("env:")[0]
        self.assertNotIn(
            "required: true",
            secrets_block,
            f"{PR_GATE}: declares a required secret. Fork PRs receive no "
            "secrets, so the gate would fail on provenance rather than on the "
            "code — mark them optional and let the tier warn instead.",
        )


class GivenASecretsScanThatWasKilled(unittest.TestCase):
    """An unrun scan is not a clean scan."""

    def test_when_gitleaks_is_oom_killed_then_it_is_retried(self):
        for name in GATE_WORKFLOWS:
            body = read_workflow(name)
            if "gitleaks" not in body:
                continue
            self.assertIn("run_gitleaks", body, f"{name}: no retry wrapper defined.")
            # Presence of the wrapper is not enough — EVERY call site must go
            # through it. An earlier version of this spec passed while one call
            # site had been reverted to a direct invocation, because the
            # function definition still matched.
            outside = strip_comments(body)
            if "run_gitleaks()" in outside:
                head, rest = outside.split("run_gitleaks()", 1)
                fn_body, tail = rest.split("\n          }", 1)
                outside = head + tail
            direct = re.findall(r"^\s*[^#\n]*\bgitleaks detect\b.*$", outside, re.M)
            self.assertEqual(
                direct, [],
                f"{name}: gitleaks is invoked directly at {direct!r}, bypassing "
                "the retry wrapper. It loads the full history into memory and "
                "gets OOM-killed (exit 137) when several repos gate at once on "
                "a 3-slot Mac Mini — three did on 2026-08-20.",
            )

    def test_when_gitleaks_is_killed_every_time_then_the_gate_fails(self):
        for name in GATE_WORKFLOWS:
            body = read_workflow(name)
            if "run_gitleaks" not in body:
                continue
            fn = body.split("run_gitleaks()")[1].split("}")[0]
            self.assertNotIn(
                "return 0\n            done",
                fn,
                f"{name}: exhausting the retries must not return success.",
            )
            self.assertIn(
                "the secrets scan did NOT run",
                body,
                f"{name}: when every attempt is killed the gate must fail "
                "loudly. A scan that never ran has found nothing — treating "
                "that as a pass is exactly the class of defect this file "
                "exists to prevent.",
            )


class GivenAStaticAnalysisPass(unittest.TestCase):
    """SAST is only worth having if an unrun scan cannot read as a clean one.

    The same trap as the gitleaks retry loop: semgrep may be absent from a
    runner image, and the easy failure mode is a step that quietly exits 0 and
    reports a green gate having analysed nothing.
    """

    def test_when_a_pull_request_is_gated_then_sast_runs(self):
        body = read_workflow(PR_GATE)
        self.assertIn(
            "semgrep",
            body,
            f"{PR_GATE}: no SAST step. gitleaks finds committed secrets, not "
            "vulnerable code — they are not substitutes for each other.",
        )

    def test_when_semgrep_is_missing_then_the_gate_says_so_rather_than_passing_silently(self):
        body = read_workflow(PR_GATE)
        self.assertIn(
            "SAST did NOT run",
            body,
            f"{PR_GATE}: when semgrep is unavailable the step must annotate "
            "that the scan did not happen. A silent exit 0 makes an unrun "
            "scan indistinguishable from a clean one.",
        )

    def test_when_sast_is_report_only_then_it_is_explicit_and_switchable(self):
        # Report-only is a deliberate phase, not an accident. It must be one
        # named switch, so turning enforcement on is a one-line change rather
        # than an archaeology exercise.
        body = read_workflow(PR_GATE)
        self.assertIn(
            "SEMGREP_BLOCKING",
            body,
            f"{PR_GATE}: SAST severity must be governed by a named flag.",
        )
        self.assertIn(
            'if [ "$SEMGREP_BLOCKING" = "true" ]',
            body,
            f"{PR_GATE}: the blocking flag must actually be honoured — a flag "
            "that is declared but never read is worse than no flag, because "
            "it reads as enforcement that is not there.",
        )

    def test_when_sast_scans_then_it_is_scoped_to_the_pull_request_diff(self):
        body = read_workflow(PR_GATE)
        self.assertIn(
            "--baseline-commit",
            body,
            f"{PR_GATE}: SAST must scan the PR diff, not the whole tree. A "
            "full-tree scan re-reports the estate's existing backlog on every "
            "PR, which is how a security step becomes noise people skip.",
        )


class GivenAFlakyRegistry(unittest.TestCase):
    """A dropped socket must not read as a broken build.

    The npm registry reset connections mid-tarball three times on 2026-08-22
    (core integration-tests, crm contract-tests, crm build-deploy). Each cost a
    green pipeline and an operator's attention for a fault that had nothing to
    do with the change under test. NPM_CONFIG_FETCH_RETRIES only covers what
    npm itself classifies as retryable; a reset during a tarball read is not
    always in that set.
    """

    def test_when_npm_install_drops_a_socket_then_it_is_retried(self):
        for name in GATE_WORKFLOWS:
            body = read_workflow(name)
            if "npm ci" not in body:
                continue
            self.assertIn(
                "npm_ci_retry",
                body,
                f"{name}: `npm ci` is called without a retry wrapper. A "
                "registry-side connection reset is not a build failure and "
                "must not be reported as one.",
            )

    def test_when_every_npm_attempt_fails_then_the_job_stops(self):
        for name in GATE_WORKFLOWS:
            body = read_workflow(name)
            if "npm_ci_retry" not in body:
                continue
            self.assertIn(
                "dependencies are NOT installed",
                body,
                f"{name}: exhausting the npm retries must fail loudly. "
                "Continuing with a partial node_modules is how a missing test "
                "runner gets reported as a passing gate.",
            )


if __name__ == "__main__":
    unittest.main()
