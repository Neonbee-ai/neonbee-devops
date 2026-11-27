"""Static contract checks for the VM agent, the templates and the deploy workflow."""
import json
import os
import re
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def read(*p):
    with open(os.path.join(ROOT, *p)) as f:
        return f.read()


class VmAgentContractTest(unittest.TestCase):
    def test_given_agent_and_ops_scripts_when_parsed_by_bash_then_no_syntax_errors(self):
        scripts = [os.path.join(ROOT, "vm-agent", "bin", f) for f in os.listdir(os.path.join(ROOT, "vm-agent", "bin"))
                   if f.endswith(".sh")]
        scripts += [os.path.join(ROOT, d, f) for d in ("scripts", "cloudflare")
                    for f in os.listdir(os.path.join(ROOT, d)) if f.endswith(".sh")]
        for s in scripts:
            r = subprocess.run(["bash", "-n", s], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, s + ": " + r.stderr)

    def test_given_agent_scripts_when_scanned_then_never_touch_all_pm2_apps(self):
        for f in os.listdir(os.path.join(ROOT, "vm-agent", "bin")):
            body = read("vm-agent", "bin", f)
            self.assertIsNone(re.search(r"pm2\S*\s+(stop|delete|restart|reload)\s+all\b", body), f)

    def test_given_deploy_script_when_reloading_then_uses_per_app_ecosystem_file_with_only(self):
        body = read("vm-agent", "bin", "nb-deploy.sh")
        self.assertIn('.nb-ecosystem.$SVC.json', body)
        self.assertIn('reload "$eco" --only "$SVC"', body)

    def test_given_compute_template_when_read_then_nb_deploy_doc_takes_service_sha_bucket_key(self):
        body = read("templates", "04-compute.yaml")
        self.assertIn("Default: nb-deploy", body)
        doc = body[body.index("DeployDocument:"):]
        for p in ("service:", "sha:", "bucket:", "key:"):
            self.assertIn("          " + p, doc)
        self.assertIn("nb-deploy.sh '{{ service }}' '{{ sha }}' '{{ bucket }}' '{{ key }}'", doc)

    def test_given_boot_script_when_read_then_reads_current_json_per_service(self):
        self.assertIn('$SVC/current.json', read("vm-agent", "bin", "nb-boot.sh"))

    def test_given_aws_nodes_when_env_rendered_then_scheduler_db_lock_is_true(self):
        self.assertIn("NB_SCHEDULER_DB_LOCK=true", read("vm-agent", "bin", "nb-lib.sh"))
        self.assertIn("NB_SCHEDULER_DB_LOCK=true", read("templates", "04-compute.yaml"))

    def test_given_service_catalog_example_when_read_then_excludes_internal_tools_vm_apps(self):
        cat = json.loads(read("vm-agent", "services.example.json"))
        names = " ".join(s["name"] for s in cat["services"])
        for banned in ("command", "website", "tools"):
            self.assertNotIn(banned, names)

    def test_given_real_catalog_when_read_then_excludes_internal_tools_vm_and_mobility_apps(self):
        # shell-fe-tools (tools.neonbee.app) moved to AWS on purpose (#152); the Internal Tools VM apps stay out
        names = [s["name"] for s in json.loads(read("vm-agent", "services.json"))["services"] if s["name"] != "shell-fe-tools"]
        for n in names:
            for banned in ("command", "website", "tools", "mobility"):
                self.assertNotIn(banned, n)

    def test_given_real_catalog_when_read_then_names_and_ports_are_unique(self):
        svcs = json.loads(read("vm-agent", "services.json"))["services"]
        names = [s["name"] for s in svcs]
        ports = [s["port"] for s in svcs if "port" in s]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(ports), len(set(ports)))

    def test_given_real_catalog_when_read_then_every_node_app_has_port_health_and_script(self):
        for s in json.loads(read("vm-agent", "services.json"))["services"]:
            self.assertIn(s.get("type"), ("node", "nginx"), s["name"])
            self.assertIsInstance(s.get("critical"), bool, s["name"])
            if s["type"] == "node":
                self.assertIsInstance(s.get("port"), int, s["name"])
                self.assertTrue(s.get("health_path", "").startswith("/"), s["name"])
                self.assertTrue(s.get("script"), s["name"])
            else:
                self.assertNotIn("port", s, s["name"])

    def test_given_real_catalog_when_read_then_front_door_apps_are_critical(self):
        by = {s["name"]: s for s in json.loads(read("vm-agent", "services.json"))["services"]}
        for n in ("nb-nginx-sites", "core-prod", "sso-fe", "shell-fe", "storefront-api-prod", "storefront-web-prod"):
            self.assertTrue(by[n]["critical"], n)

    def test_given_asg_when_read_then_starts_at_zero_with_cap_four(self):
        body = read("templates", "04-compute.yaml")
        for p, v in (("MinSize", "0"), ("DesiredCapacity", "0"), ("MaxSize", "4")):
            self.assertRegex(body, r"  %s:\n    Type: Number\n    Default: %s\n" % (p, v))

    def test_given_node_sg_when_read_then_no_ssh_and_only_cloudflare_sources(self):
        body = read("templates", "01-network.yaml")
        self.assertNotIn("FromPort: 22", body)
        self.assertEqual(body.count("SourcePrefixListId: !Ref CloudflarePrefixList"), 2)
        self.assertNotIn("CidrIp: 0.0.0.0/0\n          Description: HTTP", body)


if __name__ == "__main__":
    unittest.main()
