"""BDD tests for the night train on the nb-ci controller (launcher modes + boot contract; no network)."""
import importlib.util, os, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CTL = os.path.join(ROOT, 'runner', 'controller')
spec = importlib.util.spec_from_file_location('ntrun', os.path.join(CTL, 'night-train-run.py'))
NT = importlib.util.module_from_spec(spec); spec.loader.exec_module(NT)


def read(*p):
    return open(os.path.join(ROOT, *p)).read()


class Launcher(unittest.TestCase):

    def test_given_no_mode_then_the_tick_does_nothing(self):
        self.assertFalse(NT.plan({})[0])

    def test_given_mode_off_then_the_tick_does_nothing(self):
        self.assertFalse(NT.plan({'NT_MODE': 'off', 'NT_DEADLINE': '2026-11-27T06:00:00Z'})[0])

    def test_given_an_unknown_mode_then_the_tick_does_nothing(self):
        self.assertFalse(NT.plan({'NT_MODE': 'yes', 'NT_DEADLINE': 'x'})[0])

    def test_given_dry_without_a_deadline_then_the_tick_does_nothing(self):
        ok, why, _ = NT.plan({'NT_MODE': 'dry'})
        self.assertFalse(ok); self.assertIn('NT_DEADLINE', why)

    def test_given_dry_with_a_deadline_then_it_runs_with_DRY_1(self):
        self.assertEqual(NT.plan({'NT_MODE': 'dry', 'NT_DEADLINE': 'd'})[::2], (True, {'DRY': '1'}))

    def test_given_live_with_a_deadline_then_it_runs_with_DRY_0(self):
        self.assertEqual(NT.plan({'NT_MODE': ' LIVE ', 'NT_DEADLINE': 'd'})[::2], (True, {'DRY': '0'}))


class Contract(unittest.TestCase):
    b = read('runner', 'controller', 'bootstrap.sh')
    js = read('runner', 'controller', 'night-train.mjs')

    def test_given_boot_then_a_15_min_oneshot_timer_runs_the_launcher_as_nbci(self):
        self.assertIn('OnCalendar=*:0/15', self.b)
        self.assertIn('ExecStart=/usr/bin/python3 /opt/nbci/src/runner/controller/night-train-run.py', self.b)
        self.assertIn('Type=oneshot', self.b)

    def test_given_mode_off_then_boot_disables_the_timer(self):
        self.assertIn('systemctl disable --now night-train.timer', self.b)

    def test_given_boot_then_node_is_installed(self):
        self.assertIn('apt-get install -yq nodejs', self.b)

    def test_given_the_deadline_passed_then_later_ticks_are_no_ops(self):
        self.assertIn('if (S.finalAt)', self.js)
        self.assertIn('S.finalAt = ts()', self.js)

    def test_given_the_controller_then_no_launchctl_and_no_admin_merge(self):
        self.assertNotIn('launchctl', self.js)
        self.assertNotIn('--admin', self.js.replace('never --admin', ''))

    def test_given_secrets_then_they_are_never_hardcoded(self):
        self.assertIn("gcp.secret('nbci-pulse-token')", read('runner', 'controller', 'night-train-run.py'))
        self.assertNotIn('PULSE_TOKEN=', self.b)


if __name__ == '__main__':
    unittest.main()
