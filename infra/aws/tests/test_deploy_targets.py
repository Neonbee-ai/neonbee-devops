"""BDD: neonbee-deploy-prod `targets` switch (both|aws|ha). aws = HA deploy steps skipped, AWS artifact kept."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
WF = (ROOT / ".github/workflows/neonbee-deploy-prod.yml").read_text()
HA_STEPS = [
    "Deploy backend", "Publish swagger snapshot to Neura catalog repo", "Deploy vite-mfe", "Purge Cloudflare cache",
    "Deploy vite-mfe to HA standby", "Verify vite-mfe deploy reached a target", "Deploy nextjs (standard)",
    "Deploy nextjs to HA standby", "Verify nextjs deploy reached a target", "Deploy nextjs (standalone)",
    "Deploy nextjs standalone to HA standby", "Verify nextjs standalone deploy reached a target",
    "Enforce HA standby parity",
]


def step_block(name):
    m = re.search(r"^      - name: " + re.escape(name) + r"\n(.*?)(?=^      - name: |\Z)", WF, re.S | re.M)
    return m.group(0) if m else ""


class DeployTargetsTest(unittest.TestCase):
    def test_given_no_input_when_called_then_default_is_both(self):
        self.assertRegex(WF, r"targets:\n\s+description:.*\n\s+required: false\n\s+type: string\n\s+default: ''")
        self.assertIn("inputs.targets || vars.NB_DEPLOY_TARGETS || 'both'", WF)

    def test_given_targets_aws_when_deploying_then_every_ha_step_is_guarded(self):
        for n in HA_STEPS:
            self.assertIn("env.NB_TARGETS != 'aws'", step_block(n), n)

    def test_given_targets_aws_when_deploying_then_aws_artifact_is_still_uploaded(self):
        blk = step_block("Upload AWS release artifact")
        self.assertTrue(blk)
        self.assertNotIn("NB_TARGETS", blk)

    def test_given_default_when_pushing_then_ha_guards_stay_true(self):
        # guard is a != 'aws' test, so both/ha/unset keep every HA step as before
        self.assertNotIn("env.NB_TARGETS == 'aws'", WF)


if __name__ == "__main__":
    unittest.main()
