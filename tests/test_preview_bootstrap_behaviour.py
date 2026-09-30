"""
BDD specs — preview/bootstrap-host.sh, executed against a sandbox.

The script is copied into a temp dir with every host path (/etc/so360-preview,
/srv/previews, /etc/nginx/snippets, /etc/ssl/cloudflare, /usr/local/bin)
rewritten under that dir, and apt-get, pm2, npm, hostname, systemctl and
preview-ctl replaced by logging stubs — so nothing on the machine running the
tests is touched. Linux only (the self-hosted runner); skipped as root.
Run: python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

import os
import platform
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREVIEW = os.path.join(ROOT, "preview")
SCRIPT = os.path.join(PREVIEW, "bootstrap-host.sh")

# Host path → sandbox sub-dir. Every one must exist in the script, and none
# may survive the rewrite, or the run could reach the real filesystem.
HOST_PATHS = {
    "/etc/so360-preview": "etc/so360-preview",
    "/srv/previews": "srv/previews",
    "/etc/nginx/snippets": "etc/nginx/snippets",
    "/etc/ssl/cloudflare": "etc/ssl/cloudflare",
    "/usr/local/bin": "usr/local/bin",
}

DEV_ORIGIN = "10.0.0.1"
PROD_DB = "prodref.supabase.co"
CERT = "skyoffice360.com.crt"
KEY = "skyoffice360.com.key"

SKIP_REASON = None
if platform.system() != "Linux":
    SKIP_REASON = "bootstrap-host.sh targets Linux"
elif not shutil.which("bash"):
    SKIP_REASON = "needs bash"
elif os.geteuid() == 0:
    SKIP_REASON = "refusing to run as root (a sandbox miss could write to the host)"

STUBS = {
    "apt-get": 'echo "apt-get $*" >> "$STUB_STATE/calls.log"',
    "npm": 'echo "npm $*" >> "$STUB_STATE/calls.log"',
    "pm2": 'echo "pm2 $*" >> "$STUB_STATE/calls.log"',
    "systemctl": 'echo "systemctl $*" >> "$STUB_STATE/calls.log"',
    "preview-ctl": 'echo "preview-ctl $*" >> "$STUB_STATE/calls.log"',
    "hostname": 'echo "hostname $*" >> "$STUB_STATE/calls.log"; cat "$STUB_STATE/ips" 2>/dev/null || echo "192.168.50.7"',
}


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class GivenTheBootstrapScriptText(unittest.TestCase):
    """Order-of-operations guarantees, checked on the source."""

    text = read(SCRIPT)

    def pos(self, needle):
        i = self.text.find(needle)
        self.assertNotEqual(i, -1, f"{needle!r} not in bootstrap-host.sh")
        return i

    def test_then_it_refuses_the_dev_vm_before_installing_or_writing_anything(self):
        guard = self.pos("refusing: this is the Dev VM")
        for later in ("apt-get install", "npm install", "install -d", "preview.env <<EOF", "systemctl enable"):
            self.assertLess(guard, self.pos(later), later)

    def test_then_preview_env_pins_the_node_bin_dir_on_path(self):
        self.pos("PATH=$NODE_BIN:\\$PATH")

    def test_then_preview_env_is_made_private(self):
        self.assertLess(self.pos("preview.env <<EOF"), self.pos("chmod 600 /etc/so360-preview/preview.env"))

    def test_then_nginx_is_enabled_before_the_first_render(self):
        self.assertLess(self.pos("systemctl enable nginx"), self.pos("preview-ctl render"))

    def test_then_every_sandboxed_host_path_is_still_present(self):
        for host in HOST_PATHS:
            self.pos(host)


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class BootstrapCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="preview-bootstrap-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.p("state")
        self.bin = self.p("stubs")
        self.node_bin = self.p("node/bin")
        for d in (self.state, self.bin, self.node_bin):
            os.makedirs(d)
        # Exists on every real host; the script installs into it, never creates it.
        os.makedirs(self.p("usr/local/bin"))
        for name, body in STUBS.items():
            self.stub(self.bin, name, body)
        self.stub(self.node_bin, "node", "exit 0")

        # Sandboxed copy of the script next to the real bin/ and nginx/ dirs,
        # so $HERE resolves exactly as it does in a checkout.
        box = self.p("preview")
        os.makedirs(box)
        os.symlink(os.path.join(PREVIEW, "bin"), os.path.join(box, "bin"))
        os.symlink(os.path.join(PREVIEW, "nginx"), os.path.join(box, "nginx"))
        text = read(SCRIPT)
        for host, sub in HOST_PATHS.items():
            self.assertIn(host, text)
            text = text.replace(host, "@SANDBOX@/" + sub)
        for host in HOST_PATHS:
            # Every rewrite leaves "@SANDBOX@/<host path>", so look for a bare one.
            self.assertNotRegex(text, r"(?<!@SANDBOX@)" + re.escape(host),
                                "host path survived the sandbox rewrite")
        text = text.replace("@SANDBOX@", self.tmp)
        self.script = os.path.join(box, "bootstrap-host.sh")
        with open(self.script, "w", encoding="utf-8") as f:
            f.write(text)

    # ── helpers ────────────────────────────────────────────────────────────
    def p(self, *parts):
        return os.path.join(self.tmp, *parts)

    def stub(self, where, name, body):
        path = os.path.join(where, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\n" + body + "\n")
        os.chmod(path, 0o755)

    def certs(self):
        d = self.p("etc/ssl/cloudflare")
        os.makedirs(d, exist_ok=True)
        for n in (CERT, KEY):
            with open(os.path.join(d, n), "w") as f:
                f.write("-----BEGIN-----\n")

    def run_script(self, **overrides):
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": self.tmp,
            "STUB_STATE": self.state,
            "DEV_ORIGIN": DEV_ORIGIN,
            "PROD_SUPABASE_HOST": PROD_DB,
            "NODE_BIN": self.node_bin,
        }
        for k, v in overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        return subprocess.run(["bash", self.script], env=env, capture_output=True, text=True, timeout=60)

    def calls(self):
        try:
            return read(os.path.join(self.state, "calls.log")).splitlines()
        except FileNotFoundError:
            return []

    def env_file(self):
        return self.p("etc/so360-preview/preview.env")

    def assertRefused(self, r, message):
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn(message, r.stderr)


class GivenMissingRequiredSettings(BootstrapCase):
    def test_when_dev_origin_is_unset_then_it_refuses_before_doing_anything(self):
        r = self.run_script(DEV_ORIGIN=None)
        self.assertRefused(r, "set DEV_ORIGIN")
        self.assertEqual(self.calls(), [])

    def test_when_the_prod_supabase_host_is_unset_then_it_refuses_before_doing_anything(self):
        r = self.run_script(PROD_SUPABASE_HOST=None)
        self.assertRefused(r, "set PROD_SUPABASE_HOST")
        self.assertEqual(self.calls(), [])


class GivenTheDevVm(BootstrapCase):
    def test_when_one_of_the_host_ips_is_dev_origin_then_it_refuses_and_installs_nothing(self):
        with open(os.path.join(self.state, "ips"), "w") as f:
            f.write(f"192.168.50.7 {DEV_ORIGIN} fe80::1\n")
        r = self.run_script()
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"refusing: this is the Dev VM ({DEV_ORIGIN})", r.stderr)
        self.assertEqual(self.calls(), ["hostname -I"])
        self.assertFalse(os.path.exists(self.p("srv")))
        self.assertFalse(os.path.exists(self.env_file()))

    def test_when_an_ip_only_contains_dev_origin_as_a_prefix_then_it_is_not_the_dev_vm(self):
        with open(os.path.join(self.state, "ips"), "w") as f:
            f.write(f"{DEV_ORIGIN}0 {DEV_ORIGIN}.5\n")
        self.certs()
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)


class GivenNoNode(BootstrapCase):
    def test_when_node_bin_has_no_node_then_it_asks_for_node_22_and_writes_nothing(self):
        empty = self.p("empty")
        os.makedirs(empty)
        r = self.run_script(NODE_BIN=empty)
        self.assertRefused(r, "install Node 22 first")
        self.assertFalse(any(c.startswith(("npm ", "pm2 ")) for c in self.calls()))
        self.assertFalse(os.path.exists(self.p("srv")))


class GivenAFreshHost(BootstrapCase):
    def setUp(self):
        super().setUp()
        self.certs()

    def test_then_packages_are_installed_and_pm2_is_registered_with_systemd(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertIn("apt-get install -y -q jq nginx rsync curl util-linux", calls)
        self.assertIn("pm2 startup systemd -u root --hp /root", calls)
        self.assertFalse(any(c.startswith("npm ") for c in calls), "pm2 already on PATH")

    def test_then_the_directory_layout_preview_ctl_and_snippets_are_installed(self):
        self.assertEqual(self.run_script().returncode, 0)
        for d in ("srv/previews/_incoming", "srv/previews/_deps", "etc/so360-preview", "etc/nginx/snippets"):
            self.assertTrue(os.path.isdir(self.p(d)), d)
        ctl = self.p("usr/local/bin/preview-ctl")
        self.assertEqual(stat.S_IMODE(os.stat(ctl).st_mode), 0o755)
        snippets = sorted(n for n in os.listdir(os.path.join(PREVIEW, "nginx")) if n.endswith(".conf"))
        self.assertTrue(snippets)
        for n in snippets:
            self.assertEqual(stat.S_IMODE(os.stat(self.p("etc/nginx/snippets", n)).st_mode), 0o644, n)

    def test_then_preview_env_records_the_node_bin_dir_dev_origin_and_prod_host(self):
        self.assertEqual(self.run_script().returncode, 0)
        lines = read(self.env_file()).splitlines()
        self.assertEqual(lines[0], f"PATH={self.node_bin}:$PATH")
        self.assertIn(f"DEV_ORIGIN={DEV_ORIGIN}", lines)
        self.assertIn(f"PROD_SUPABASE_HOST={PROD_DB}", lines)
        self.assertIn("# PREVIEW_PORT_MIN=7100", lines)
        self.assertEqual(stat.S_IMODE(os.stat(self.env_file()).st_mode), 0o600)

    def test_when_node_bin_is_not_given_then_it_is_found_from_node_on_path(self):
        self.stub(self.bin, "node", "exit 0")
        r = self.run_script(NODE_BIN=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read(self.env_file()).splitlines()[0], f"PATH={self.bin}:$PATH")

    def test_then_nginx_is_enabled_at_boot_before_the_first_render(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertIn("systemctl enable nginx", calls)
        self.assertIn("preview-ctl render", calls)
        self.assertLess(calls.index("systemctl enable nginx"), calls.index("preview-ctl render"))
        self.assertIn("preview host ready", r.stdout)

    def test_when_systemctl_enable_fails_then_the_bootstrap_still_completes(self):
        self.stub(self.bin, "systemctl", 'echo "systemctl $*" >> "$STUB_STATE/calls.log"; exit 1')
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("preview-ctl render", self.calls())


class GivenAnAlreadyBootstrappedHost(BootstrapCase):
    def test_when_rerun_then_the_existing_preview_env_is_kept_verbatim(self):
        self.certs()
        os.makedirs(self.p("etc/so360-preview"))
        custom = "PATH=/opt/node/bin:$PATH\nDEV_ORIGIN=10.0.0.1\nPROD_SUPABASE_HOST=prodref.supabase.co\nPREVIEW_PORT_MAX=7200\n"
        with open(self.env_file(), "w") as f:
            f.write(custom)
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read(self.env_file()), custom)

    def test_when_run_twice_then_the_second_run_also_succeeds(self):
        self.certs()
        self.assertEqual(self.run_script().returncode, 0)
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls().count("preview-ctl render"), 2)


class GivenNoOriginCertificate(BootstrapCase):
    def test_when_the_cert_is_missing_then_it_stops_before_rendering(self):
        r = self.run_script()
        self.assertRefused(r, "missing Cloudflare origin cert")
        self.assertIn("copy it from the Dev VM", r.stderr)
        self.assertNotIn("preview-ctl render", self.calls())
        self.assertNotIn("systemctl enable nginx", self.calls())

    def test_when_the_key_is_empty_then_it_stops_before_rendering(self):
        self.certs()
        open(self.p("etc/ssl/cloudflare", KEY), "w").close()
        r = self.run_script()
        self.assertRefused(r, "missing Cloudflare origin cert")
        self.assertNotIn("preview-ctl render", self.calls())

    def test_when_preview_env_points_at_another_cert_then_that_path_is_checked(self):
        self.certs()
        os.makedirs(self.p("etc/so360-preview"))
        with open(self.env_file(), "w") as f:
            f.write(f"PREVIEW_SSL_CERT={self.p('nowhere.crt')}\n")
        r = self.run_script()
        self.assertRefused(r, self.p("nowhere.crt"))


if __name__ == "__main__":
    unittest.main()
