"""BDD: the nginx sites symlink must not live under sites-enabled (glob include hits a directory)."""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
IMAGE = (ROOT / "infra/aws/templates/06-image.yaml").read_text()
DEPLOY = (ROOT / "infra/aws/vm-agent/bin/nb-deploy.sh").read_text()


class NginxSitesLayoutTest(unittest.TestCase):
    def test_given_ami_template_when_linking_sites_then_link_is_outside_sites_enabled(self):
        self.assertIn("/etc/nginx/nb-sites", IMAGE)
        self.assertNotIn("ln -sfn /opt/neonbee/services/nb-nginx-sites/current/sites /etc/nginx/sites-enabled", IMAGE)

    def test_given_ami_template_when_patching_nginx_conf_then_includes_nb_sites_conf(self):
        self.assertIn("include /etc/nginx/nb-sites/*.conf;", IMAGE)

    def test_given_old_layout_node_when_deploying_nginx_then_self_heals(self):
        self.assertIn("rm -f /etc/nginx/sites-enabled/neonbee", DEPLOY)
        self.assertIn("nb-sites/*.conf", DEPLOY)


if __name__ == "__main__":
    unittest.main()
