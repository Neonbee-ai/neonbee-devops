"""BDD: /opt/neonbee (the neonbee user's home) must be traversable by nginx, or FE hosts return 500."""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
IMAGE = (ROOT / "infra/aws/templates/06-image.yaml").read_text()
BOOT = (ROOT / "infra/aws/vm-agent/bin/nb-boot.sh").read_text()


class OptNeonbeeModeTest(unittest.TestCase):
    def test_given_ami_template_when_creating_neonbee_home_then_mode_is_711(self):
        self.assertIn("useradd --system --create-home --home-dir /opt/neonbee", IMAGE)
        self.assertIn("chmod 711 /opt/neonbee", IMAGE)

    def test_given_node_boot_when_starting_then_it_repairs_home_mode_before_deploying(self):
        self.assertIn("chmod 711 /opt/neonbee", BOOT)
        self.assertLess(BOOT.index("chmod 711 /opt/neonbee"), BOOT.index("nb_fetch_catalog"))


if __name__ == "__main__":
    unittest.main()
