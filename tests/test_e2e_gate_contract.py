"""
BDD specs — the post-deploy E2E gate is report-only, Contabo-only and never
points at production. Run: python3 -m unittest discover tests
"""

import os
import re
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, ".github", "workflows", "e2e-gate.yml")


def load():
    with open(GATE, encoding="utf-8") as f:
        text = f.read()
    return text, yaml.safe_load(text)


def call_inputs(doc):
    # PyYAML reads the bare `on:` key as boolean True.
    on = doc.get("on", doc.get(True))
    return on["workflow_call"]


def url_guard(text):
    m = re.search(r'\[\[ "\$URL" =~ (\S+) \]\]', text)
    assert m, "target_url guard not found"
    return re.compile(m.group(1))


class GivenTheE2EGateWorkflow(unittest.TestCase):
    def setUp(self):
        self.text, self.doc = load()

    def test_when_called_then_it_is_a_reusable_workflow_with_the_four_inputs(self):
        inputs = call_inputs(self.doc)["inputs"]
        for name in ("target_url", "changed_modules", "pr_number", "tiers"):
            self.assertIn(name, inputs)

    def test_when_blocking_is_not_passed_then_it_is_report_only(self):
        blocking = call_inputs(self.doc)["inputs"]["blocking"]
        self.assertEqual(blocking["type"], "boolean")
        self.assertIs(blocking["default"], False)

    def test_then_every_job_runs_on_the_pinned_contabo_browser_fleet(self):
        for name, job in self.doc["jobs"].items():
            self.assertEqual(job["runs-on"], ["self-hosted", "Linux", "X64", "contabo", "browsers"], name)
        self.assertNotIn("ubuntu-latest", self.text)

    def test_then_secrets_are_declared_never_inlined(self):
        declared = set(call_inputs(self.doc)["secrets"])
        used = set(re.findall(r"secrets\.([A-Z0-9_]+)", self.text))
        self.assertTrue(used <= declared, used - declared)
        self.assertNotIn("E2E_ADMIN_", self.text)

    def test_when_target_is_a_preview_or_dev_then_it_is_accepted(self):
        guard = url_guard(self.text)
        for ok in ("https://pr-feat-x.skyoffice360.com", "https://pr-a--sso.skyoffice360.com/",
                   "https://dev-dashboard.skyoffice360.com"):
            self.assertTrue(guard.search(ok), ok)

    def test_when_target_is_production_or_foreign_then_it_is_refused(self):
        guard = url_guard(self.text)
        for bad in ("https://dashboard.neonbee.app", "https://pr-x.skyoffice360.com.evil.io",
                    "http://dev-dashboard.skyoffice360.com", "https://dev-dashboard.skyoffice360.com/x"):
            self.assertFalse(guard.search(bad), bad)

    def test_then_the_pr_comment_is_updated_in_place_by_a_marker(self):
        self.assertIn("<!-- neonbee-e2e-gate -->", self.text)
        self.assertIn("PATCH", self.text)


if __name__ == "__main__":
    unittest.main()
