
The Mac Minis are reserved for autonomous QA, and the Dev VM (dev-vm-01 also
carries the `contabo` label) only receives dist — it never builds.
A `route` job (on the dedicated nb-route runners) picks the lane for the
heavy jobs: `[self-hosted, runner-vm]` or `[self-hosted, lane-burst]`.
"""
import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")))
# Housekeeping that must reach every Linux runner's own workspace.
EVERY_RUNNER = {"cleanup-runner-workspace.yml"}
RUNNER_VM = "[self-hosted, runner-vm]"
ROUTER = "[self-hosted, nb-route]"
ROUTED = "${{ fromJSON(needs.route.outputs.runs_on) }}"
# nb-pool: label on Contabo runner-vm + burst runners, used only for NB_BURST=batch re-runs (either lane).
LANES = {'["self-hosted","runner-vm"]', '["self-hosted","lane-burst"]', '["self-hosted","nb-pool"]'}


def runs_on(path):
    with open(path) as f:
        return re.findall(r"runs-on: (.*)", f.read())


class GivenTheOrgWorkflows(unittest.TestCase):
    def test_when_a_job_picks_a_runner_then_it_targets_the_runner_vm_label(self):
        for path in WORKFLOWS:
            name = os.path.basename(path)
            if name in EVERY_RUNNER:
                continue
            for target in runs_on(path):
                with self.subTest(workflow=name):
                    self.assertIn(target.strip(), (RUNNER_VM, ROUTER, ROUTED))

    def test_when_a_job_is_routed_then_its_workflow_has_a_route_job_on_the_router(self):
        for path in WORKFLOWS:
            text = open(path).read()
            if ROUTED not in text:
                continue
            with self.subTest(workflow=os.path.basename(path)):
                self.assertRegex(text, r"\n  route:\n(?:    .*\n)*?    runs-on: \[self-hosted, nb-route\]")

    def test_when_a_job_is_routed_then_it_needs_the_route_job_directly(self):
        for path in WORKFLOWS:
            text = open(path).read()
            for job in re.split(r"\n(?=  [A-Za-z0-9_-]+:\n)", text):
                if ROUTED in job:
                    with self.subTest(workflow=os.path.basename(path), job=job.split(":")[0].strip()):
                        self.assertRegex(job, r"\n    needs: \[route(, [^\]]+)?\]")

    def test_when_the_router_picks_a_lane_then_it_is_the_runner_vm_or_burst(self):
        for path in WORKFLOWS:
            text = open(path).read()
            if ROUTED not in text:
                continue
            with self.subTest(workflow=os.path.basename(path)):
                self.assertEqual(set(re.findall(r"lane='(\[[^']*\])'", text)), LANES)

    def test_given_a_rerun_or_a_failed_previous_run_then_the_router_keeps_contabo(self):
        for path in WORKFLOWS:
            text = open(path).read()
            if ROUTED not in text:
                continue
            with self.subTest(workflow=os.path.basename(path)):
                self.assertIn('elif [ "$ATTEMPT" != 1 ]; then why="re-run attempt $ATTEMPT"', text)
                self.assertIn('failure) why="previous run of this workflow on $BRANCH failed"', text)
                self.assertIn("lane='[\"self-hosted\",\"runner-vm\"]'; why=", text)

    def test_given_nb_burst_batch_then_first_runs_go_to_burst_and_reruns_to_the_shared_pool(self):
        for path in WORKFLOWS:
            text = open(path).read()
            if ROUTED not in text:
                continue
            with self.subTest(workflow=os.path.basename(path)):
                first = text.index('if [ "${MODE:-off}" = batch ] && [ "$ATTEMPT" = 1 ]; then lane=\'["self-hosted","lane-burst"]\'')
                rerun = text.index('elif [ "${MODE:-off}" = batch ]; then lane=\'["self-hosted","nb-pool"]\'')
                self.assertLess(first, rerun)
                self.assertLess(rerun, text.index('elif [ "$ATTEMPT" != 1 ]'))

    def test_given_the_run_history_cannot_be_read_then_the_router_keeps_contabo(self):
        for path in WORKFLOWS:
            text = open(path).read()
            if ROUTED not in text:
                continue
            with self.subTest(workflow=os.path.basename(path)):
                self.assertIn('unknown) why="run history unavailable - staying on Contabo"', text)

    def test_when_the_router_reads_run_history_then_it_resolves_the_caller_workflow_from_this_run(self):
        for path in WORKFLOWS:
            text = open(path).read()
            if ROUTED not in text:
                continue
            with self.subTest(workflow=os.path.basename(path)):
                self.assertIn('"$api/runs/$GITHUB_RUN_ID"', text)
                self.assertIn('.workflow_id', text)
                self.assertNotIn('${{ github.workflow_ref }}', text)

    def test_when_a_caller_grants_no_permissions_then_the_router_asks_for_none(self):
        for path in WORKFLOWS:
            text = open(path).read()
            if ROUTED not in text:
                continue
            with self.subTest(workflow=os.path.basename(path)):
                route = re.search(r"\n  route:\n((?:    .*\n|\n)*)", text).group(1)
                self.assertNotIn("permissions:", route)  # extra scopes → startup_failure in callers

    def test_when_a_job_picks_a_runner_then_it_never_uses_the_shared_contabo_label(self):
        for path in WORKFLOWS:
            with self.subTest(workflow=os.path.basename(path)):
                self.assertNotIn("contabo", " ".join(runs_on(path)))


if __name__ == "__main__":
    unittest.main()
