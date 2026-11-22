"""BDD: deploying nginx sites on a node whose nginx is not running must start it, not fail on reload."""
import pathlib
import unittest

DEPLOY = (pathlib.Path(__file__).resolve().parents[3] / "infra/aws/vm-agent/bin/nb-deploy.sh").read_text()


class NginxDeployStartsInactiveTest(unittest.TestCase):
    def test_given_inactive_nginx_when_deploying_sites_then_falls_back_to_restart(self):
        self.assertIn("systemctl reload nginx", DEPLOY)
        self.assertIn("systemctl restart nginx", DEPLOY)
        self.assertLess(DEPLOY.index("systemctl reload nginx"), DEPLOY.index("systemctl restart nginx"))

    def test_given_script_when_parsed_then_syntax_ok(self):
        import subprocess
        self.assertEqual(subprocess.run(["bash", "-n", str(pathlib.Path(__file__).resolve().parents[3] / "infra/aws/vm-agent/bin/nb-deploy.sh")]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
