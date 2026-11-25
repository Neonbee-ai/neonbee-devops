"""BDD checks for the nb-nginx-sites generator (infra/aws/nginx/gen_sites.py)."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "nginx"))
import gen_sites  # noqa: E402

CAT = gen_sites.load_catalog()
FILES = gen_sites.render_all(CAT)


def servers(text):
    return re.findall(r"server_name ([^;]+);", text)


class NginxSitesTest(unittest.TestCase):
    def test_given_catalog_when_generated_then_every_prod_host_has_a_site_file(self):
        names = set(FILES)
        for f in ("api", "dashboard", "sso", "cdn", "sign", "portal", "partner", "mcp"):
            self.assertIn("%s.neonbee.app.conf" % f, names)
        self.assertIn("neonbee.app.conf", names)
        self.assertIn("neonbee-store.conf", names)

    def test_given_free_tools_catalog_entry_when_generated_then_tools_app_serves_shell_fe_tools_spa(self):
        tools = FILES["tools.neonbee.app.conf"]
        self.assertIn("server_name tools.neonbee.app;", tools)
        self.assertIn("root /opt/neonbee/services/shell-fe-tools/current/dist;", tools)
        self.assertIn("try_files $uri $uri/ /index.html;", tools)
        self.assertIn("/etc/ssl/cloudflare/neonbee.app.crt", tools)

    def test_given_dev_command_website_tools_hosts_when_generated_then_excluded(self):
        blob = "\n".join(FILES.values())
        for bad in ("skyoffice360", "command.neonbee", "tools.neonbee.ai", "neonbee.ai", "moonhive", "pulse.neonbee", "app.neonbee.app"):
            self.assertNotIn(bad, blob)

    def test_given_gateway_when_generated_then_every_catalog_backend_has_module_route_on_catalog_port(self):
        api = FILES["api.neonbee.app.conf"]
        for mod, svc, _ in gen_sites.GATEWAY:
            self.assertIn("location /%s/ {" % mod, api)
            self.assertIn("upstream nb_%s { server 127.0.0.1:%d;" % (svc.replace("-", "_"), CAT[svc]["port"]),
                          FILES["00-nb-http.conf"])
        self.assertEqual(len(re.findall(r"^    location /\w+/ \{", api, re.M)), len(gen_sites.GATEWAY))

    def test_given_mobility_not_in_catalog_when_generated_then_no_mobility_route(self):
        self.assertNotIn("/mobility/", FILES["api.neonbee.app.conf"])

    def test_given_websocket_modules_when_generated_then_upgrade_headers_set(self):
        api = FILES["api.neonbee.app.conf"]
        for mod in ("inbox", "support", "chat"):
            blk = api.split("location /%s/ {" % mod)[1].split("\n    }")[0]
            self.assertIn("Upgrade $http_upgrade", blk)

    def test_given_every_server_when_generated_then_lb_health_snippet_included_on_80_and_443(self):
        for name, body in FILES.items():
            if name.startswith("00-"):
                continue
            n_servers = len(re.findall(r"^server \{", body, re.M))
            self.assertEqual(body.count("nb-lb-health.conf"), n_servers, name)

    def test_given_http_conf_when_generated_then_cloudflare_real_ip_configured(self):
        http = FILES["00-nb-http.conf"]
        self.assertIn("set_real_ip_from 173.245.48.0/20;", http)
        self.assertIn("real_ip_header CF-Connecting-IP;", http)

    def test_given_cdn_when_generated_then_every_static_mfe_served_from_its_dist_with_spa_fallback(self):
        cdn = FILES["cdn.neonbee.app.conf"]
        prefixes = gen_sites.cdn_prefixes(CAT)
        self.assertGreaterEqual(len(prefixes), 24)
        for p, svc in prefixes:
            self.assertIn("location /%s/ {\n        alias /opt/neonbee/services/%s/current/dist/;" % (p, svc), cdn)
            self.assertIn("try_files $uri $uri/ /%s/index.html;" % p, cdn)
        self.assertIn(("contact-support", "help-fe"), prefixes)
        self.assertIn(("people", "people-connect-fe"), prefixes)

    def test_given_cdn_when_generated_then_remote_entry_never_cached_and_assets_immutable_with_cors(self):
        cdn = FILES["cdn.neonbee.app.conf"]
        re_blocks = re.findall(r"location ~\* \^/[a-z-]+/assets/remoteEntry\\\.js\$ \{[^}]*\}", cdn)
        self.assertEqual(len(re_blocks), len(gen_sites.cdn_prefixes(CAT)))
        for b in re_blocks:
            self.assertIn('add_header Cache-Control "no-cache" always;', b)
            self.assertNotIn("no-store", b)  # no-store defeats ETag/304 revalidation
            self.assertIn('Access-Control-Allow-Origin "*"', b)
        self.assertIn("$nb_asset_cache_control", cdn)
        self.assertIn('add_header Access-Control-Allow-Origin "*" always;', cdn)
        self.assertLess(cdn.index("remoteEntry"), cdn.index("$nb_asset_cache_control"))  # regex order

    def test_given_asset_cache_map_when_generated_then_immutable_only_on_success_and_errors_no_store(self):
        http = FILES["00-nb-http.conf"]
        self.assertIn("map $status $nb_asset_cache_control {", http)
        self.assertIn('~^[23]  "public, max-age=31536000, immutable";', http)
        self.assertIn('default "no-store";', http)

    def test_given_any_site_when_generated_then_no_literal_immutable_header_or_duplicate_expires_on_mfe_hosts(self):
        for f in ("cdn.neonbee.app.conf", "dashboard.neonbee.app.conf", "sso.neonbee.app.conf"):
            body = FILES[f]
            self.assertNotIn('"public, immutable"', body, f)  # would also stamp 404s
            self.assertNotIn("expires 1y;", body, f)  # second Cache-Control header

    def test_given_remote_stylesheet_at_base_root_when_requested_then_falls_back_to_assets_copy(self):
        cdn = FILES["cdn.neonbee.app.conf"]
        for p, svc in gen_sites.cdn_prefixes(CAT):
            block = re.search(r"location ~\* \^/%s/\(\[\^/\]\+\\\.css\)\$ \{[^}]*\}" % re.escape(p), cdn)
            self.assertIsNotNone(block, p)
            self.assertIn("root /opt/neonbee/services/%s/current/dist;" % svc, block.group(0))
            self.assertIn("try_files /$1 /assets/$1 =404;", block.group(0))
            # must precede the generic alias location, which would 404 the root path
            self.assertLess(cdn.index(block.group(0)), cdn.index("location ~* ^/%s/(.+\\.(?:" % p))

    def test_given_shell_spa_when_generated_then_unhashed_manifests_revalidate_before_asset_rule(self):
        for f in ("dashboard.neonbee.app.conf", "sso.neonbee.app.conf"):
            body = FILES[f]
            m = re.search(r"location ~\* \^/\(\?:index[^{]*\{[^}]*\}", body)
            self.assertIsNotNone(m, f)
            for name in ("index\\.html", "sw\\.js", "manifest\\.json"):
                self.assertIn(name, m.group(0))
            self.assertIn('add_header Cache-Control "no-cache" always;', m.group(0))
            # regex locations match in order: sw.js must not hit the immutable *.js rule
            self.assertLess(body.index(m.group(0)), body.index("$nb_asset_cache_control"))

    def test_given_shell_and_sso_when_generated_then_spa_roots_are_their_dist_and_host_has_no_cdn_paths(self):
        for f, svc in (("dashboard.neonbee.app.conf", "shell-fe"), ("sso.neonbee.app.conf", "sso-fe")):
            body = FILES[f]
            self.assertIn("root /opt/neonbee/services/%s/current/dist;" % svc, body)
            self.assertIn("try_files $uri $uri/ /index.html;", body)
            self.assertIn("remoteEntry", body)

    def test_given_critical_static_catalog_entries_when_generated_then_server_name_matches_catalog(self):
        for svc, f in (("shell-fe", "dashboard.neonbee.app.conf"), ("sso-fe", "sso.neonbee.app.conf")):
            self.assertIn("server_name %s;" % CAT[svc]["server_name"], FILES[f])

    def test_given_next_apps_when_generated_then_proxied_to_catalog_ports(self):
        self.assertIn("proxy_pass http://127.0.0.1:%d;" % CAT["partner-portal-prod"]["port"], FILES["partner.neonbee.app.conf"])
        store = FILES["neonbee-store.conf"]
        self.assertIn("server_name *.neonbee.store neonbee.store customers-fallback.neonbee.store;", store)
        self.assertIn("proxy_pass http://127.0.0.1:%d;" % CAT["storefront-web-prod"]["port"], store)
        self.assertIn("location /v1/ {\n        proxy_pass http://127.0.0.1:%d;" % CAT["storefront-api-prod"]["port"], store)

    def test_given_sign_public_when_generated_then_s_prefix_maps_to_dist_and_root_redirects(self):
        sign = FILES["sign.neonbee.app.conf"]
        self.assertIn("location /s/ {\n        alias /opt/neonbee/services/sign-public-fe/current/dist/;", sign)
        self.assertIn("location = / { return 302 https://sign.neonbee.app/s/; }", sign)

    def test_given_mcp_when_generated_then_proxies_neura_upstream_unbuffered(self):
        mcp = FILES["mcp.neonbee.app.conf"]
        self.assertIn("proxy_pass http://nb_neura_prod;", mcp)
        self.assertIn("proxy_buffering off;", mcp)

    def test_given_generated_conf_when_scanned_then_braces_balance_and_statements_terminated(self):
        for name, body in FILES.items():
            code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
            code = re.sub(r"'[^']*'|\"[^\"]*\"", "''", code)
            self.assertEqual(code.count("{"), code.count("}"), name)
            for line in code.splitlines():
                s = line.strip()
                if s and not s.endswith((";", "{", "}")):
                    self.fail("%s: unterminated line: %s" % (name, s))

    def test_given_no_listen_443_without_cert_when_generated_then_every_tls_server_has_cert_and_key(self):
        for name, body in FILES.items():
            self.assertEqual(body.count("listen 443 ssl"), body.count("ssl_certificate "), name)
            self.assertEqual(body.count("ssl_certificate "), body.count("ssl_certificate_key "), name)

    def test_given_committed_sites_when_regenerated_then_no_drift(self):
        with tempfile.TemporaryDirectory() as d:
            gen_sites.main(["--out", d])
            sites = os.path.join(ROOT, "nginx", "sites")
            self.assertEqual(sorted(os.listdir(d)), sorted(os.listdir(sites)))
            for f in os.listdir(d):
                self.assertEqual(open(os.path.join(d, f)).read(), open(os.path.join(sites, f)).read(), f)

    def test_given_scripts_when_parsed_by_bash_then_no_syntax_errors(self):
        for s in (("nginx", "ensure-tls.sh"), ("scripts", "package-nginx-sites.sh"), ("vm-agent", "bin", "nb-deploy.sh")):
            r = subprocess.run(["bash", "-n", os.path.join(ROOT, *s)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_given_nginx_activation_when_deploying_then_tls_hook_runs_before_config_test(self):
        body = open(os.path.join(ROOT, "vm-agent", "bin", "nb-deploy.sh")).read()
        self.assertLess(body.index("ensure-tls.sh"), body.index("nginx -t -q && systemctl reload nginx"))

    def test_given_ensure_tls_when_scanned_then_never_echoes_key_material(self):
        body = open(os.path.join(ROOT, "nginx", "ensure-tls.sh")).read()
        self.assertNotRegex(body, r"echo[^\n]*\$json|printf[^\n]*\$key\b[^>]*$")
        self.assertIn("chmod 0600", body)

    def test_given_package_script_when_dry_run_then_bundle_has_sites_hook_and_release_marker(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run(["bash", os.path.join(ROOT, "scripts", "package-nginx-sites.sh"), "--sha", "testsha1"],
                               cwd=d, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            f = [x for x in os.listdir(d) if x.startswith("nb-nginx-sites-testsha1")][0]
            listing = subprocess.run("(zstd -dc %s 2>/dev/null || gzip -dc %s) | tar -tf -" % (f, f), cwd=d, shell=True,
                                     capture_output=True, text=True).stdout
            for want in ("sites/api.neonbee.app.conf", "ensure-tls.sh", "nb-release.json"):
                self.assertIn(want, listing)
            self.assertNotIn("gen_sites.py", listing)


if __name__ == "__main__":
    unittest.main()
