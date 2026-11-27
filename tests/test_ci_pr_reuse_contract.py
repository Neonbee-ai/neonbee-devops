"""BDD specs - CI speed programme (Pulse 652ffcd8): reuse a PR's unit+security result on the merge commit."""
import json
import os
import re
import subprocess
import tempfile
import textwrap
import unittest

ROOT = os.path.join(os.path.dirname(__file__), '..')
WF = os.path.join(ROOT, '.github', 'workflows')
VITE = 'neonbee-deploy-vite-mfe.yml'
BACKEND = 'neonbee-deploy-backend.yml'
FE_JOBS = [('unit', 'success'), ('security', 'success')]
BE_JOBS = FE_JOBS + [('API Contract Tests', 'success'), ('Integration Tests', 'success')]


def prior_script(wf):
    t = open(os.path.join(WF, wf)).read()
    body = t.split('      - id: prior\n')[1].split('        run: |\n')[1].split('\n\n')[0]
    return textwrap.dedent(body)


class GivenAPushToDevelopAndAPrRun(unittest.TestCase):
    """The route 'prior' step, run for real against a stub GitHub API (curl shim)."""

    def run_step(self, wf, kind, tree_push, tree_pr, jobs):
        d = tempfile.mkdtemp()
        out = os.path.join(d, 'out')
        responses = {
            'commits/PUSH': {'commit': {'tree': {'sha': tree_push}}},
            'actions/runs?event=pull_request&status=completed&per_page=40':
                {'workflow_runs': [{'id': 9, 'head_sha': 'PRHEAD', 'conclusion': 'success'}]},
            'commits/PRHEAD': {'commit': {'tree': {'sha': tree_pr}}},
            'actions/runs/9/jobs?filter=latest&per_page=100':
                {'jobs': [{'name': 'ci / ' + n, 'conclusion': c} for n, c in jobs]},
        }
        rj = os.path.join(d, 'r.json')
        open(rj, 'w').write(json.dumps(responses))
        shim = os.path.join(d, 'curl')
        open(shim, 'w').write(
            '#!/usr/bin/env python3\nimport sys, json\nr = json.load(open(%r))\n'
            'u = sys.argv[-1].split("/repos/o/r/")[1]\n'
            'print(json.dumps(r[u])) if u in r else sys.exit(22)\n' % rj)
        os.chmod(shim, 0o755)
        env = dict(os.environ, PATH=d + ':' + os.environ['PATH'], GH_TOKEN='t', SHA='PUSH', KIND=kind,
                   GITHUB_API_URL='https://x', GITHUB_REPOSITORY='o/r', GITHUB_OUTPUT=out,
                   GITHUB_STEP_SUMMARY=os.path.join(d, 'sum'))
        r = subprocess.run(['bash', '-c', prior_script(wf)], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return open(out).read().strip()

    def test_when_the_trees_match_and_every_tier_passed_then_the_tiers_are_reused(self):
        self.assertEqual(self.run_step(VITE, 'frontend', 'T1', 'T1', FE_JOBS), 'prior_pass=true')
        self.assertEqual(self.run_step(BACKEND, 'backend', 'T1', 'T1', BE_JOBS), 'prior_pass=true')

    def test_when_the_trees_differ_then_nothing_is_reused(self):
        self.assertEqual(self.run_step(VITE, 'frontend', 'T1', 'T2', FE_JOBS), 'prior_pass=false')

    def test_when_a_tier_failed_or_is_missing_then_nothing_is_reused(self):
        self.assertEqual(self.run_step(VITE, 'frontend', 'T1', 'T1', [('unit', 'success'), ('security', 'failure')]), 'prior_pass=false')
        self.assertEqual(self.run_step(VITE, 'frontend', 'T1', 'T1', [('unit', 'success')]), 'prior_pass=false')

    def test_when_a_backend_pr_skipped_a_tier_then_nothing_is_reused(self):
        jobs = [('unit', 'success'), ('security', 'success'), ('API Contract Tests', 'skipped'), ('Integration Tests', 'success')]
        self.assertEqual(self.run_step(BACKEND, 'backend', 'T1', 'T1', jobs), 'prior_pass=false')


class GivenTheDeployBuildThatFollowsSkippedTiers(unittest.TestCase):
    def test_when_a_tier_is_skipped_then_build_proceeds_only_with_prior_pass_and_a_failure_always_blocks(self):
        for wf, tiers in ((VITE, ['gate', 'security']), (BACKEND, ['gate', 'security', 'contract-tests', 'integration-tests'])):
            t = open(os.path.join(WF, wf)).read()
            cond = re.search(r"\n  build-deploy:\n(?:.*\n)*?    if: (.*)\n", t).group(1)
            for j in tiers:
                self.assertIn("needs.%s.result == 'success' || (needs.%s.result == 'skipped' && needs.route.outputs.prior_pass == 'true')" % (j, j), cond, (wf, j))
            self.assertIn('!cancelled()', cond)
            self.assertNotIn("result != 'failure'", cond)

    def test_when_reuse_is_not_opted_in_then_it_never_runs_and_never_off_develop_or_on_reruns(self):
        for wf in (VITE, BACKEND):
            t = open(os.path.join(WF, wf)).read()
            cond = re.search(r"- id: prior\n        if: (.*)\n", t).group(1)
            for need in ("vars.NB_PR_REUSE == 'on'", 'github.run_attempt == 1', "github.ref == 'refs/heads/develop'", "github.event_name == 'push'"):
                self.assertIn(need, cond, wf)


if __name__ == '__main__':
    unittest.main()
