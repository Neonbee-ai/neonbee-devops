"""BDD: the Notify Teams job of the reusable deploy workflows must use the shared nb-route label, not the single runner-vm host."""
import pathlib
import re
import unittest

WF = pathlib.Path(__file__).resolve().parents[3] / ".github/workflows"
FILES = ["neonbee-deploy-prod.yml", "neonbee-deploy-backend.yml", "neonbee-deploy-nextjs.yml",
         "neonbee-deploy-vite-mfe.yml", "neonbee-rollback.yml"]


def notify_block(text):
    m = re.search(r"(?m)^  [A-Za-z0-9_-]+:\n(?:(?!^  [A-Za-z0-9_-]+:\n).*\n)*?    name: Notify Teams\n(?:(?!^  [A-Za-z0-9_-]+:\n).*\n)*", text)
    return m.group(0) if m else ""


class NotifyRunsOnTest(unittest.TestCase):
    def test_given_deploy_workflows_when_notifying_then_runs_on_nb_route(self):
        for f in FILES:
            blk = notify_block((WF / f).read_text())
            self.assertTrue(blk, f)
            self.assertIn("runs-on: [self-hosted, nb-route]", blk, f)
            self.assertNotIn("runner-vm", blk, f)


if __name__ == "__main__":
    unittest.main()
