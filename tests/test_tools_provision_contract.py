"""
BDD specs — tools.neonbee.app is provisioned through CI only.

The Free Tools dashboard (so360-shell-fe, tools flavor) gets its own vhost on
the Prod VM and the HA VM plus a Cloudflare LB/record, all from
neonbee-tools-provision.yml. Run: python3 -m unittest discover tests
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF = os.path.join(ROOT, "nginx", "tools.neonbee.app.conf")
WF = os.path.join(ROOT, ".github", "workflows", "neonbee-tools-provision.yml")


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class GivenTheToolsVhost(unittest.TestCase):
    def setUp(self):
        self.body = read(CONF)

    def test_then_it_serves_only_tools_neonbee_app_from_its_own_web_root(self):
        self.assertEqual(set(re.findall(r"server_name\s+([^;]+);", self.body)), {"tools.neonbee.app"})
        self.assertIn("root /var/www/tools.neonbee.app/html;", self.body)

    def test_then_it_uses_the_cloudflare_origin_cert_present_on_both_vms(self):
        self.assertIn("ssl_certificate /etc/ssl/cloudflare/neonbee.app.crt;", self.body)
        self.assertIn("ssl_certificate_key /etc/ssl/cloudflare/neonbee.app.key;", self.body)

    def test_then_spa_routes_fall_back_to_an_uncached_index(self):
        self.assertIn("try_files $uri $uri/ /index.html;", self.body)
        self.assertRegex(self.body, r'location = /index\.html \{[^}]*no-cache')

    def test_when_an_asset_404s_then_the_immutable_header_is_not_attached(self):
        for m in re.finditer(r'add_header\s+Cache-Control\s+"([^"]*)"\s*(always)?\s*;', self.body):
            if "immutable" in m.group(1):
                self.assertIsNone(m.group(2), "immutable Cache-Control must not use `always`")


class GivenTheProvisionWorkflow(unittest.TestCase):
    def setUp(self):
        self.body = read(WF)

    def test_then_it_is_manual_and_runs_only_on_the_contabo_runner(self):
        self.assertRegex(self.body, r"on:\s*\n\s*workflow_dispatch:")
        self.assertNotIn("ubuntu-latest", self.body)
        runners = re.findall(r"runs-on:\s*(.+)", self.body)
        self.assertTrue(runners)
        for r in runners:
            self.assertEqual(r.strip(), "[self-hosted, contabo, Linux]")

    def test_then_it_installs_the_vhost_on_prod_and_ha(self):
        self.assertIn("nginx/tools.neonbee.app.conf", self.body)
        self.assertIn('for IP in "$PROD_IP" "$HA_IP"', self.body)
        self.assertIn("secrets.HA_VM_IP", self.body)

    def test_when_nginx_t_fails_then_the_vhost_is_rolled_back(self):
        self.assertIn("if nginx -t; then", self.body)
        self.assertIn("rolling back", self.body)

    def test_then_cloudflare_is_idempotent_and_proxied(self):
        self.assertIn("already exists", self.body)
        self.assertEqual(self.body.count('\\"proxied\\":true'), 2)

    def test_then_it_supports_a_dry_run_that_changes_nothing(self):
        self.assertIn("dry_run", self.body)
        self.assertIn("[DRY RUN]", self.body)

    def test_then_it_never_builds_or_ships_source_on_a_vm(self):
        self.assertNotIn("npm run build", self.body)
        self.assertNotIn("npm ci", self.body)


if __name__ == "__main__":
    unittest.main()
