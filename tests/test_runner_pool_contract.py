"""Every CI job builds on the runner VM only.

The Mac Minis are reserved for autonomous QA, and the Dev VM (dev-vm-01 also
carries the `contabo` label) only receives dist — it never builds.
"""
import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")))
# Housekeeping that must reach every Linux runner's own workspace.
EVERY_RUNNER = {"cleanup-runner-workspace.yml"}


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
                    self.assertEqual(target.strip(), "[self-hosted, runner-vm]")

    def test_when_a_job_picks_a_runner_then_it_never_uses_the_shared_contabo_label(self):
        for path in WORKFLOWS:
            with self.subTest(workflow=os.path.basename(path)):
                self.assertNotIn("contabo", " ".join(runs_on(path)))


if __name__ == "__main__":
    unittest.main()
