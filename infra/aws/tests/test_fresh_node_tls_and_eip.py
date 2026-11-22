"""BDD: a fresh ASG node (instance replacement / scale-out) gets the Cloudflare Origin CA certs
and lands on a SERVING pool Elastic IP, so Cloudflare origins pinned to IPs stay valid."""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BIN = os.path.join(ROOT, "vm-agent", "bin")
TOOLS = all(shutil.which(t) for t in ("jq", "openssl"))


def read(*p):
    with open(os.path.join(ROOT, *p)) as f:
        return f.read()


def write_exe(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def make_pair(d, cn):
    crt, key = os.path.join(d, cn + ".crt"), os.path.join(d, cn + ".key")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj",
                    "/CN=" + cn, "-keyout", key, "-out", crt], check=True, capture_output=True)
    return read_abs(crt), read_abs(key)


def read_abs(p):
    with open(p) as f:
        return f.read()


@unittest.skipUnless(TOOLS, "jq and openssl are required")
class FreshNodeTlsTest(unittest.TestCase):
    """ensure-tls.sh is the nb-nginx-sites post-extract hook (nb-deploy.sh runs it before nginx -t)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "root")
        self.stub = os.path.join(self.tmp, "bin")
        os.makedirs(self.root)
        os.makedirs(self.stub)
        self.secrets = {}
        for name, cn in (("neonbee-app", "origin-app"), ("neonbee-store", "origin-store")):
            crt, key = make_pair(self.tmp, cn)
            self.secrets[name] = {"crt": crt, "key": key}
            with open(os.path.join(self.tmp, name + ".json"), "w") as f:
                json.dump(self.secrets[name], f)
        # aws stub: `secretsmanager get-secret-value --secret-id <prefix>/tls/<name>` -> SecretString
        write_exe(os.path.join(self.stub, "aws"),
                  '#!/bin/bash\nwhile [ $# -gt 0 ]; do [ "$1" = --secret-id ] && id=$2; shift; done\n'
                  'f="$SECRETS_DIR/${id##*/}.json"; [ -f "$f" ] && cat "$f" || exit 254\n')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_tls(self):
        env = dict(os.environ, PATH=self.stub + os.pathsep + os.environ["PATH"], NB_TLS_ROOT=self.root,
                   SECRETS_DIR=self.tmp)
        return subprocess.run(["bash", os.path.join(ROOT, "nginx", "ensure-tls.sh")], env=env,
                              capture_output=True, text=True)

    def installed(self, rel):
        return read_abs(os.path.join(self.root, rel))

    def test_given_fresh_node_without_certs_when_ensure_tls_runs_then_origin_ca_pairs_installed(self):
        r = self.run_tls()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.installed("etc/ssl/cloudflare/neonbee.app.crt"), self.secrets["neonbee-app"]["crt"])
        self.assertEqual(self.installed("etc/ssl/cloudflare/neonbee.app.key"), self.secrets["neonbee-app"]["key"])
        self.assertEqual(self.installed("etc/ssl/certs/neonbee-store.crt"), self.secrets["neonbee-store"]["crt"])
        self.assertEqual(self.installed("etc/ssl/private/neonbee-store.key"), self.secrets["neonbee-store"]["key"])
        self.assertNotIn("self-signed", r.stderr)
        self.assertEqual(oct(os.stat(os.path.join(self.root, "etc/ssl/private/neonbee-store.key")).st_mode & 0o777), "0o600")

    def test_given_self_signed_placeholder_when_ensure_tls_runs_then_secret_pair_replaces_it(self):
        d = os.path.join(self.root, "etc/ssl/cloudflare")
        os.makedirs(d)
        crt, key = make_pair(self.tmp, "placeholder")
        for ext, body in (("crt", crt), ("key", key)):
            with open(os.path.join(d, "neonbee.app." + ext), "w") as f:
                f.write(body)
        r = self.run_tls()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.installed("etc/ssl/cloudflare/neonbee.app.crt"), self.secrets["neonbee-app"]["crt"])

    def test_given_secret_key_not_matching_cert_when_ensure_tls_runs_then_existing_pair_kept(self):
        d = os.path.join(self.root, "etc/ssl/cloudflare")
        os.makedirs(d)
        crt, key = make_pair(self.tmp, "existing")
        for ext, body in (("crt", crt), ("key", key)):
            with open(os.path.join(d, "neonbee.app." + ext), "w") as f:
                f.write(body)
        _, other_key = make_pair(self.tmp, "other")
        with open(os.path.join(self.tmp, "neonbee-app.json"), "w") as f:
            json.dump({"crt": self.secrets["neonbee-app"]["crt"], "key": other_key}, f)
        r = self.run_tls()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.installed("etc/ssl/cloudflare/neonbee.app.crt"), crt)
        self.assertIn("does not match", r.stderr)


SERVING = [{"Key": "nb-pool", "Value": "nb-prod"}, {"Key": "nb-serving", "Value": "true"}]
SPARE = [{"Key": "nb-pool", "Value": "nb-prod"}]


def addr(alloc, tags, instance=None):
    a = {"AllocationId": alloc, "PublicIp": alloc, "Tags": tags}
    if instance:
        a.update(InstanceId=instance, AssociationId="assoc-" + alloc)
    return a


@unittest.skipUnless(TOOLS, "jq and openssl are required")
class EipClaimTest(unittest.TestCase):
    ME = "i-new"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.stub = os.path.join(self.tmp, "bin")
        os.makedirs(self.stub)
        self.log = os.path.join(self.tmp, "aws.log")
        # IMDS stub: token PUT, then instance-id.
        write_exe(os.path.join(self.stub, "curl"), '#!/bin/bash\n[[ "$*" == *instance-id* ]] && echo "$ME_ID"; exit 0\n')
        write_exe(os.path.join(self.stub, "sleep"), "#!/bin/sh\nexit 0\n")
        # Portable stand-ins (the CI runner may be macOS): identity shuf, pass-through timeout.
        write_exe(os.path.join(self.stub, "shuf"), "#!/bin/sh\ncat\n")
        write_exe(os.path.join(self.stub, "timeout"), '#!/bin/sh\nshift\nexec "$@"\n')
        # aws stub: describe-addresses -> fixture; associate-address logs and fails for $DENY ids.
        write_exe(os.path.join(self.stub, "aws"),
                  '#!/bin/bash\nif [[ "$*" == *describe-addresses* ]]; then cat "$FIXTURE"; exit 0; fi\n'
                  'while [ $# -gt 0 ]; do [ "$1" = --allocation-id ] && id=$2; shift; done\n'
                  'echo "associate $id" >> "$AWS_LOG"; [[ " $DENY " == *" $id "* ]] && exit 1; exit 0\n')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_eip(self, mode, addresses, deny=""):
        fx = os.path.join(self.tmp, "fx.json")
        with open(fx, "w") as f:
            json.dump({"Addresses": addresses}, f)
        env = dict(os.environ, PATH=self.stub + os.pathsep + os.environ["PATH"], ME_ID=self.ME, FIXTURE=fx,
                   AWS_LOG=self.log, DENY=deny, NB_CONF=os.path.join(self.tmp, "none.conf"),
                   NB_STATE=os.path.join(self.tmp, "state"), NB_LOG_DIR=os.path.join(self.tmp, "log"))
        r = subprocess.run(["bash", os.path.join(BIN, "nb-eip.sh"), mode], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return read_abs(self.log).split() [1::2] if os.path.exists(self.log) else []

    def test_given_replacement_node_and_freed_serving_eip_when_boot_then_claims_serving_not_spare(self):
        calls = self.run_eip("boot", [addr("spare-1", SPARE), addr("serve-a", SERVING),
                                      addr("serve-b", SERVING, "i-other"), addr("spare-3", SPARE)])
        self.assertEqual(calls, ["serve-a"])

    def test_given_both_serving_eips_held_when_boot_then_claims_a_spare(self):
        calls = self.run_eip("boot", [addr("serve-a", SERVING, "i-old"), addr("serve-b", SERVING, "i-other"),
                                      addr("spare-1", SPARE)])
        self.assertEqual(calls, ["spare-1"])

    def test_given_serving_eip_taken_by_another_node_in_a_race_when_boot_then_tries_next_serving(self):
        calls = self.run_eip("boot", [addr("serve-a", SERVING), addr("serve-b", SERVING), addr("spare-1", SPARE)],
                             deny="serve-a")
        self.assertEqual(calls[-1], "serve-b")
        self.assertNotIn("spare-1", calls)

    def test_given_node_on_spare_and_serving_eip_frees_up_when_reconcile_then_moves_to_serving(self):
        calls = self.run_eip("reconcile", [addr("spare-1", SPARE, self.ME), addr("serve-a", SERVING),
                                           addr("serve-b", SERVING, "i-other")])
        self.assertEqual(calls, ["serve-a"])

    def test_given_node_on_serving_eip_when_reconcile_then_never_moved(self):
        calls = self.run_eip("reconcile", [addr("serve-a", SERVING, self.ME), addr("serve-b", SERVING)])
        self.assertEqual(calls, [])

    def test_given_node_on_spare_and_no_free_serving_when_reconcile_then_no_spare_hopping(self):
        calls = self.run_eip("reconcile", [addr("spare-1", SPARE, self.ME), addr("spare-2", SPARE),
                                           addr("serve-a", SERVING, "i-x"), addr("serve-b", SERVING, "i-y")])
        self.assertEqual(calls, [])

    def test_given_rebooted_node_on_spare_when_boot_then_does_not_hop_to_another_spare(self):
        calls = self.run_eip("boot", [addr("spare-1", SPARE, self.ME), addr("spare-2", SPARE),
                                      addr("serve-a", SERVING, "i-x")])
        self.assertEqual(calls, [])


class FreshNodeWiringTest(unittest.TestCase):
    def test_given_boot_when_claiming_eip_then_uses_serving_first_claimer_before_deploying(self):
        boot = read("vm-agent", "bin", "nb-boot.sh")
        self.assertIn('nb-eip.sh" boot', boot)
        self.assertNotIn("Addresses[?AssociationId==`null`]", boot)
        self.assertLess(boot.index('nb-eip.sh" boot'), boot.index("nb_fetch_catalog &&"))

    def test_given_health_timer_when_on_a_real_node_then_reconciles_eip_at_most_every_60s(self):
        health = read("vm-agent", "bin", "nb-health.sh")
        self.assertIn('nb-eip.sh" reconcile', health)
        self.assertIn('-n "${NB_RELEASE_BUCKET:-}"', health)
        self.assertIn("-ge 60", health)

    def test_given_network_template_when_read_then_exactly_two_serving_pool_eips(self):
        net = read("templates", "01-network.yaml")
        self.assertEqual(net.count("Key: nb-serving, Value: 'true'"), 2)
        for n in ("PoolEip2:", "PoolEip4:"):   # 3.110.17.10 and 13.233.75.233 = CF pool origins
            block = net[net.index(n):].split("\n\n", 1)[0]
            self.assertIn("nb-serving", block)

    def test_given_launch_template_when_node_boots_then_agent_hot_updated_before_boot_service(self):
        ud = read("templates", "04-compute.yaml")
        self.assertIn("agent/vm-agent.tar.gz", ud)
        self.assertLess(ud.index("agent/vm-agent.tar.gz"), ud.index("systemctl enable --now nb-boot.service"))
        self.assertIn('( for f in "$NBA"/bin/*.sh; do bash -n "$f" || exit 1; done )', ud)
        self.assertIn("baked agent kept", ud)

    def test_given_node_role_when_read_then_may_associate_only_pool_tagged_eips(self):
        ud = read("templates", "04-compute.yaml")
        self.assertIn("'aws:ResourceTag/nb-pool'", ud)


if __name__ == "__main__":
    unittest.main()
