"""BDD specs - CI speed programme (Pulse 652ffcd8): shallow clones where history is never read."""
import os, re, unittest

ROOT = os.path.join(os.path.dirname(__file__), '..')
WF = os.path.join(ROOT, '.github', 'workflows')


def jobs(name):
    t = open(os.path.join(WF, name)).read()
    parts = re.split(r'\n  ([a-z][a-z-]*):\n', t)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


class GivenAUnitOrGateTier(unittest.TestCase):
    def test_when_it_checks_out_then_it_is_shallow_unless_the_repo_opts_out(self):
        for wf, job in (('neonbee-pr-gate.yml', 'unit'), ('neonbee-deploy-vite-mfe.yml', 'gate'), ('neonbee-deploy-backend.yml', 'gate')):
            body = jobs(wf)[job]
            self.assertIn("fetch-depth: ${{ vars.NB_FULL_CLONE == '1' && 0 || 1 }}", body, (wf, job))

    def test_when_it_is_shallow_then_it_runs_no_git_history_command(self):
        for wf, job in (('neonbee-pr-gate.yml', 'unit'), ('neonbee-deploy-vite-mfe.yml', 'gate'), ('neonbee-deploy-backend.yml', 'gate')):
            self.assertNotRegex(jobs(wf)[job], r'git (log|rev-list|describe|merge-base|diff)\b', (wf, job))


class GivenASecurityTier(unittest.TestCase):
    def test_when_it_scans_secrets_then_it_keeps_the_full_history(self):
        for wf in ('neonbee-pr-gate.yml', 'neonbee-deploy-vite-mfe.yml', 'neonbee-deploy-backend.yml'):
            self.assertIn('fetch-depth: 0', jobs(wf)['security'], wf)


if __name__ == '__main__':
    unittest.main()
