"""BDD behaviour tests: catalog args/env -> PM2 ecosystem, and nb-health probing critical static bundles."""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BIN = os.path.join(ROOT, "vm-agent", "bin")


def catalog():
    with open(os.path.join(ROOT, "vm-agent", "services.json")) as f:
        return {s["name"]: s for s in json.load(f)["services"]}


def slurp(path):
    with open(path) as f:
        return f.read()


def write_exe(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


@unittest.skipUnless(shutil.which("node"), "node is required")
class Pm2ApplyArgsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.eco = os.path.join(self.tmp, "eco.json")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def apply(self, jlist="[]", **extra):
        env = dict(os.environ, NB_NAME="partner-portal-prod", NB_SCRIPT="/svc/current/node_modules/next/dist/bin/next",
                   NB_CWD="/svc/current", NB_ECO=self.eco, NB_PORT="8046", NB_EXEC="fork", **extra)
        r = subprocess.run(["node", os.path.join(BIN, "nb-pm2-apply.js")], input=jlist, env=env,
                           capture_output=True, text=True)
        app = json.loads(slurp(self.eco))["apps"][0] if os.path.exists(self.eco) else None
        return r, app

    def test_given_catalog_args_when_ecosystem_written_then_pm2_args_and_port_env_are_set(self):
        r, app = self.apply(NB_ARGS='["start","-p","8046"]')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(app["args"], ["start", "-p", "8046"])
        self.assertEqual(app["env"]["PORT"], "8046")
        self.assertEqual(app["cwd"], "/svc/current")

    def test_given_no_args_when_ecosystem_written_then_args_key_is_absent(self):
        r, app = self.apply(NB_ARGS="null")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("args", app)

    def test_given_catalog_env_when_ecosystem_written_then_merged_but_port_and_node_env_win(self):
        r, app = self.apply(NB_ENV='{"HOSTNAME":"0.0.0.0","PORT":"1","NODE_ENV":"dev"}')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(app["env"]["HOSTNAME"], "0.0.0.0")
        self.assertEqual(app["env"]["PORT"], "8046")
        self.assertEqual(app["env"]["NODE_ENV"], "production")

    def test_given_args_not_a_string_array_when_applied_then_exits_non_zero_without_writing(self):
        r, app = self.apply(NB_ARGS='"start -p 8046"')
        self.assertNotEqual(r.returncode, 0)
        self.assertIsNone(app)

    def test_given_running_app_with_same_args_when_applied_then_reload(self):
        jl = json.dumps([{"name": "partner-portal-prod", "pm2_env": {
            "exec_mode": "fork_mode", "pm_cwd": "/svc/current",
            "pm_exec_path": "/svc/current/node_modules/next/dist/bin/next", "args": ["start", "-p", "8046"]}}])
        r, _ = self.apply(jl, NB_ARGS='["start","-p","8046"]')
        self.assertEqual(r.stdout.strip(), "reload")

    def test_given_running_app_with_different_args_when_applied_then_restart(self):
        jl = json.dumps([{"name": "partner-portal-prod", "pm2_env": {
            "exec_mode": "fork_mode", "pm_cwd": "/svc/current",
            "pm_exec_path": "/svc/current/node_modules/next/dist/bin/next", "args": ["start", "-p", "3034"]}}])
        r, _ = self.apply(jl, NB_ARGS='["start","-p","8046"]')
        self.assertEqual(r.stdout.strip(), "restart")


class NextAppsCatalogTest(unittest.TestCase):
    def test_given_partner_portal_when_read_then_starts_with_next_start_on_its_port(self):
        s = catalog()["partner-portal-prod"]
        self.assertEqual(s["script"], "node_modules/next/dist/bin/next")
        self.assertEqual(s["args"], ["start", "-p", str(s["port"])])
        self.assertTrue(s["install"])

    def test_given_storefront_web_standalone_when_read_then_runs_standalone_server_on_all_interfaces(self):
        s = catalog()["storefront-web-prod"]
        self.assertEqual(s["script"], ".next/standalone/server.js")
        self.assertEqual(s["env"]["HOSTNAME"], "0.0.0.0")

    def test_given_any_service_args_or_env_when_read_then_types_are_strings(self):
        for n, s in catalog().items():
            if "args" in s:
                self.assertTrue(all(isinstance(a, str) for a in s["args"]), n)
            if "env" in s:
                self.assertTrue(all(isinstance(v, str) for v in s["env"].values()), n)

    def test_given_deploy_script_when_read_then_passes_args_env_and_stages_standalone_assets(self):
        body = slurp(os.path.join(BIN, "nb-deploy.sh"))
        self.assertIn('NB_ARGS="$(nb_svc_json "$SVC" args)"', body)
        self.assertIn('NB_ENV="$(nb_svc_json "$SVC" env)"', body)
        self.assertIn('"$target/.next/standalone/.next/"', body)

    def test_given_critical_static_entries_when_read_then_have_server_name(self):
        for n, s in catalog().items():
            if s.get("static") and s["critical"]:
                self.assertTrue(s.get("server_name"), n)


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "jq and bash are required")
class HealthCriticalStaticTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state")
        self.stub = os.path.join(self.tmp, "bin")
        os.makedirs(self.state)
        os.makedirs(self.stub)
        with open(os.path.join(self.state, "boot.done"), "w"):
            pass
        self.curl_log = os.path.join(self.tmp, "curl.log")
        write_exe(os.path.join(self.stub, "systemctl"), "#!/bin/sh\nexit 0\n")
        # curl stub: log the call; fail when any argument contains $FAIL_MATCH.
        write_exe(os.path.join(self.stub, "curl"),
                  '#!/bin/bash\necho "$*" >> "$CURL_LOG"\n'
                  'for a in "$@"; do [ -n "$FAIL_MATCH" ] && [[ "$a" == *"$FAIL_MATCH"* ]] && exit 22; done\nexit 0\n')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_health(self, services, fail=""):
        with open(os.path.join(self.state, "services.json"), "w") as f:
            json.dump({"services": services}, f)
        env = dict(os.environ, PATH=self.stub + os.pathsep + os.environ["PATH"], NB_STATE=self.state,
                   NB_CONF=os.path.join(self.tmp, "none.conf"), NB_LOG_DIR=os.path.join(self.tmp, "log"),
                   CURL_LOG=self.curl_log, FAIL_MATCH=fail)
        r = subprocess.run(["bash", os.path.join(BIN, "nb-health.sh")], env=env, capture_output=True, text=True)
        health = json.loads(slurp(os.path.join(self.state, "health.json")))
        calls = slurp(self.curl_log).splitlines() if os.path.exists(self.curl_log) else []
        return r, health, calls

    SHELL = {"name": "shell-fe", "type": "nginx", "static": True, "critical": True,
             "server_name": "dashboard.neonbee.app", "health_path": "/index.html"}

    def test_given_critical_static_bundle_served_when_health_runs_then_probed_via_nginx_host_and_node_healthy(self):
        r, h, calls = self.run_health([self.SHELL])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(h["ok"])
        self.assertEqual(len(calls), 1)
        self.assertIn("Host: dashboard.neonbee.app", calls[0])
        self.assertIn("http://127.0.0.1:80/index.html", calls[0])
        self.assertTrue(os.path.exists(os.path.join(self.state, "www", "__lb_health")))

    def test_given_critical_static_bundle_missing_when_health_runs_then_node_unhealthy(self):
        r, h, _ = self.run_health([self.SHELL], fail="dashboard.neonbee.app")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(h["ok"])
        self.assertIn("shell-fe down", h["reasons"])
        self.assertFalse(os.path.exists(os.path.join(self.state, "www", "__lb_health")))

    def test_given_critical_static_without_server_name_when_health_runs_then_node_unhealthy(self):
        svc = {k: v for k, v in self.SHELL.items() if k != "server_name"}
        _, h, calls = self.run_health([svc])
        self.assertFalse(h["ok"])
        self.assertIn("shell-fe has no server_name", h["reasons"])
        self.assertEqual(calls, [])

    def test_given_non_critical_static_and_nginx_entries_when_health_runs_then_not_probed(self):
        _, h, calls = self.run_health([
            {"name": "nb-nginx-sites", "type": "nginx", "critical": True},
            {"name": "crm-fe", "type": "nginx", "static": True, "critical": False, "server_name": "x"}])
        self.assertTrue(h["ok"])
        self.assertEqual(calls, [])

    def test_given_static_without_health_path_when_health_runs_then_defaults_to_index_html(self):
        svc = {k: v for k, v in self.SHELL.items() if k != "health_path"}
        _, _, calls = self.run_health([svc])
        self.assertIn("/index.html", calls[0])

    def test_given_node_app_after_static_entry_when_health_runs_then_port_and_path_are_not_shifted(self):
        _, h, calls = self.run_health([self.SHELL, {"name": "core-prod", "type": "node", "port": 8000,
                                                    "health_path": "/api/health", "critical": True}])
        self.assertTrue(h["ok"])
        self.assertIn("http://127.0.0.1:8000/api/health", calls[1])

    def test_given_critical_node_app_down_when_health_runs_then_node_unhealthy(self):
        _, h, _ = self.run_health([{"name": "core-prod", "type": "node", "port": 8000, "critical": True}],
                                  fail=":8000/")
        self.assertFalse(h["ok"])
        self.assertIn("core-prod", h["down"])


if __name__ == "__main__":
    unittest.main()
