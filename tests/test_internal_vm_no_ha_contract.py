"""
BDD spec - Internal Tools VM deploys have no HA standby (2026-11-22).

Given the old HA VM 84.247.131.148 is retired from prod (Dev + preview only),
When a caller deploys with target_vm=internal (neonbee-website, command-be),
Then neonbee-deploy-prod.yml ships to the Internal Tools VM only and never
deploys a "<service>-ha" copy to HA_VM_IP; prod/storefront keep their HA logic.
"""
import os
import re
import unittest

WF = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "neonbee-deploy-prod.yml")


class InternalVmNoHaTest(unittest.TestCase):
    def setUp(self):
        with open(WF) as f:
            self.src = f.read()

    def test_given_target_vm_internal_when_backend_deploys_then_ha_fan_out_excludes_it(self):
        line = next(l for l in self.src.splitlines() if 'deploy_vm "$HA_IP"' in l or ('if [ -n "$HA_IP" ]' in l))
        self.assertNotIn('"internal"', line)
        self.assertIn('"storefront"', line)

    def test_given_target_vm_internal_when_any_ha_condition_is_evaluated_then_internal_is_absent(self):
        self.assertIsNone(re.search(r'\[ "\$TARGET_VM" = "internal" \]', self.src))
        self.assertIsNone(re.search(r"inputs\.target_vm == 'internal'", self.src))
        self.assertNotIn("prod|storefront|internal)", self.src)

    def test_given_target_vm_internal_when_resolving_primary_then_internal_tools_vm_is_used(self):
        self.assertIn("internal)   echo \"ip=${{ secrets.INTERNAL_TOOLS_VM }}\"", self.src)


if __name__ == "__main__":
    unittest.main()
