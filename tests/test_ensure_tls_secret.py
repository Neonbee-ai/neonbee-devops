"""BDD: infra/aws/nginx/ensure-tls.sh installs the Secrets Manager origin cert over a placeholder."""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/aws/nginx/ensure-tls.sh"
COMPUTE = (ROOT / "infra/aws/templates/04-compute.yaml").read_text()


def pair(d, name):
    crt, key = os.path.join(d, name + ".crt"), os.path.join(d, name + ".key")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj",
                    "/CN=" + name, "-keyout", key, "-out", crt], check=True, capture_output=True)
    return open(crt).read(), open(key).read()


@unittest.skipUnless(shutil.which("openssl") and shutil.which("jq"), "needs openssl + jq")
class EnsureTlsSecretTest(unittest.TestCase):
    def run_with_secret(self, secret):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        root, bin_ = os.path.join(d, "root"), os.path.join(d, "bin")
        os.makedirs(bin_)
        with open(os.path.join(d, "secret.json"), "w") as fh:
            fh.write(secret)
        with open(os.path.join(bin_, "aws"), "w") as fh:  # stub: same secret for both names
            fh.write(f"#!/bin/sh\ncat '{d}/secret.json'\n")
        os.chmod(os.path.join(bin_, "aws"), 0o755)
        env = dict(os.environ, PATH=bin_ + os.pathsep + os.environ["PATH"], NB_TLS_ROOT=root)
        return d, root, env, None

    def test_given_placeholder_and_valid_secret_when_run_then_secret_pair_replaces_it(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        crt, key = pair(d, "origin")
        _, root, env, _ = self.run_with_secret(json.dumps({"crt": crt, "key": key}))
        target = os.path.join(root, "etc/ssl/cloudflare/neonbee.app.crt")
        # Seed a self-signed placeholder first (no secret readable).
        os.makedirs(os.path.dirname(target), exist_ok=True)
        pc, pk = pair(d, "placeholder")
        open(target, "w").write(pc)
        open(target[:-4] + ".key", "w").write(pk)
        r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(open(target).read().strip(), crt.strip())
        self.assertEqual(oct(os.stat(target[:-4] + ".key").st_mode & 0o777), "0o600")
        self.assertNotIn("PRIVATE KEY", r.stderr + r.stdout)

    def test_given_secret_key_not_matching_cert_when_run_then_installed_pair_is_kept(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        crt, _ = pair(d, "a")
        _, other_key = pair(d, "b")
        _, root, env, _ = self.run_with_secret(json.dumps({"crt": crt, "key": other_key}))
        target = os.path.join(root, "etc/ssl/cloudflare/neonbee.app.crt")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        pc, pk = pair(d, "placeholder")
        open(target, "w").write(pc)
        open(target[:-4] + ".key", "w").write(pk)
        r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(open(target).read(), pc)
        self.assertIn("does not match", r.stderr)


class NodeRoleTest(unittest.TestCase):
    def test_given_compute_template_when_read_then_nodes_may_read_tls_secrets(self):
        self.assertIn("secret:neonbee/prod/tls/*", COMPUTE)


if __name__ == "__main__":
    unittest.main()
