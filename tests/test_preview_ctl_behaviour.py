"""
BDD specs — preview-ctl behaviour, executed for real against a throw-away root.

test_preview_contract.py pins the text of the guard rails; these specs run
preview/bin/preview-ctl end to end (deploy / remove / sleep / wake / deps /
prune / render / cf-ensure / cf-remove) with pm2, curl, ss, nginx, systemctl, flock, install and sleep
replaced by logging stubs, so every branch of the script is exercised without
touching a real host.

Needs Linux (GNU sed -i, bash 4+) and jq — exactly what the self-hosted runner
has. Skipped elsewhere. The host config is redirected via PREVIEW_ENV_FILE to a
sandbox path, so the specs also run on the runner that doubles as the preview
host without reading /etc/so360-preview/preview.env.
Run: python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

import json
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CTL = os.path.join(ROOT, "preview", "bin", "preview-ctl")

HASH = "0123456789abcdef"
SLUG = "feat-crm-owner"
BRANCH = "feat/CRM-owner"


def _gnu_sed():
    try:
        return subprocess.run(["sed", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


SKIP_REASON = None
if platform.system() != "Linux":
    SKIP_REASON = "preview-ctl targets Linux (GNU sed -i)"
elif not shutil.which("jq") or not shutil.which("bash") or not _gnu_sed():
    SKIP_REASON = "needs bash, jq and GNU sed"


# Stubs log every call to $STUB_STATE/calls.log. Behaviour is switched by
# marker files in $STUB_STATE: curl_fail, nginx_fail, nginx_active, busy_ports.
STUBS = {
    "pm2": r'''
echo "pm2 $* PORT=${PORT:-}" >> "$STUB_STATE/calls.log"
on="$STUB_STATE/online"; touch "$on"
case "$1" in
  start)
    name=""; prev=""
    for a in "$@"; do [ "$prev" = --name ] && name=$a; prev=$a; done
    echo "$name" >> "$on" ;;
  stop|delete)
    grep -vx -- "$2" "$on" > "$on.tmp" || true; mv "$on.tmp" "$on" ;;
  describe)
    if grep -qx -- "$2" "$on"; then echo "| status | online |"; else echo "| status | stopped |"; fi ;;
esac
exit 0
''',
    "curl": r'''
echo "curl $*" >> "$STUB_STATE/calls.log"
[ -e "$STUB_STATE/curl_fail" ] && exit 22
# Cloudflare API fake (PREVIEW_CF_API=https://cf.test): -K - carries the token.
case " $* " in *" -K - "*) cat >> "$STUB_STATE/curl_config" ;; esac
url=""; for a in "$@"; do case "$a" in https://cf.test/*) url=$a ;; esac; done
case " $* " in *" -X "*) exit 0 ;; esac
case "$url" in
  *"/zones?name="*)       echo '{"result":[{"id":"Z1"}]}' ;;
  *"/access/apps?"*)      cat "$STUB_STATE/cf_apps" 2>/dev/null || echo '{"result":[]}' ;;
  *"/dns_records?"*)      cat "$STUB_STATE/cf_dns" 2>/dev/null || echo '{"result":[]}' ;;
esac
exit 0
''',
    "ss": r'''
echo "ss $*" >> "$STUB_STATE/calls.log"
last="${@: -1}"; p="${last##*:}"
if [ -f "$STUB_STATE/busy_ports" ] && grep -qx -- "$p" "$STUB_STATE/busy_ports"; then
  echo "LISTEN 0 511 0.0.0.0:$p 0.0.0.0:*"
fi
exit 0
''',
    "nginx": r'''
echo "nginx $*" >> "$STUB_STATE/calls.log"
if [ -e "$STUB_STATE/nginx_fail" ]; then echo "nginx: [emerg] stub failure" >&2; exit 1; fi
exit 0
''',
    "systemctl": r'''
echo "systemctl $*" >> "$STUB_STATE/calls.log"
if [ "$1" = is-active ]; then [ -e "$STUB_STATE/nginx_active" ]; exit $?; fi
exit 0
''',
    "flock": 'echo "flock $*" >> "$STUB_STATE/calls.log"; exit 0\n',
    "install": 'echo "install $*" >> "$STUB_STATE/calls.log"; exit 0\n',
    "sleep": 'echo "sleep $*" >> "$STUB_STATE/calls.log"; exit 0\n',
}


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class PreviewCtlCase(unittest.TestCase):
    """Sandbox: a fresh preview root, nginx output and stub PATH per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pvctl-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "previews")
        self.state = os.path.join(self.tmp, "state")
        self.bin = os.path.join(self.tmp, "bin")
        self.nginx_out = os.path.join(self.tmp, "nginx", "so360-previews.conf")
        for d in (self.state, self.bin, os.path.dirname(self.nginx_out)):
            os.makedirs(d)
        for name, body in STUBS.items():
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write("#!/usr/bin/env bash\n" + body)
            os.chmod(path, 0o755)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("PREVIEW_") and k not in ("DEV_ORIGIN", "PROD_SUPABASE_HOST", "PORT")}
        env.update({
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "STUB_STATE": self.state,
            "PREVIEW_ROOT": self.root,
            "PREVIEW_DOMAIN": "skyoffice360.com",
            "PREVIEW_NGINX_OUT": self.nginx_out,
            "PREVIEW_SSL_CERT": "/certs/preview.crt",
            "PREVIEW_SSL_KEY": "/certs/preview.key",
            "PREVIEW_PORT_MIN": "7100",
            "PREVIEW_PORT_MAX": "7105",
            "DEV_ORIGIN": "10.0.0.1",
            "PREVIEW_ENV_FILE": os.path.join(self.tmp, "etc", "preview.env"),
            "PREVIEW_CI_ENV_FILE": os.path.join(self.tmp, "etc", "ci.env"),
            "PREVIEW_CF_ENV_FILE": os.path.join(self.tmp, "etc", "cf.env"),
            "PREVIEW_CF_API": "https://cf.test",
        })
        self.env = env

    # ── harness ──────────────────────────────────────────────────────────────
    def ctl(self, *args, env=None):
        run_env = dict(self.env)
        for k, v in (env or {}).items():
            if v is None:
                run_env.pop(k, None)
            else:
                run_env[k] = v
        return subprocess.run(["bash", CTL, *args], env=run_env, capture_output=True, text=True, timeout=120)

    def ok(self, *args, env=None):
        r = self.ctl(*args, env=env)
        self.assertEqual(r.returncode, 0, f"preview-ctl {' '.join(args)} failed:\n{r.stdout}\n{r.stderr}")
        return r

    def fails(self, *args, message, env=None, code=1):
        r = self.ctl(*args, env=env)
        self.assertEqual(r.returncode, code, f"expected exit {code}:\n{r.stdout}\n{r.stderr}")
        self.assertIn(message, r.stderr)
        return r

    def mark(self, name, content=""):
        with open(os.path.join(self.state, name), "w") as f:
            f.write(content)

    def unmark(self, name):
        path = os.path.join(self.state, name)
        if os.path.exists(path):
            os.remove(path)

    def calls(self):
        path = os.path.join(self.state, "calls.log")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return f.read().splitlines()

    def clear_calls(self):
        self.unmark("calls.log")

    def registry(self):
        with open(os.path.join(self.root, "registry.json")) as f:
            return json.load(f)

    def write_registry(self, data):
        os.makedirs(self.root, exist_ok=True)
        with open(os.path.join(self.root, "registry.json"), "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def nginx(self):
        with open(self.nginx_out) as f:
            return f.read()

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def upload_fe(self, repo, slug=SLUG, content="v1"):
        d = self.p("_incoming", slug, repo)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "remoteEntry.js"), "w") as f:
            f.write(content)

    def upload_be(self, repo, slug=SLUG, env_text="SUPABASE_URL=https://preview-db.supabase.co\n",
                  main=True, env_file=True, deps="upload", lockhash=HASH):
        d = self.p("_incoming", slug, repo)
        os.makedirs(os.path.join(d, "dist", "src"), exist_ok=True)
        if main:
            with open(os.path.join(d, "dist", "src", "main.js"), "w") as f:
                f.write("// main\n")
        if env_file:
            with open(os.path.join(d, ".env"), "w") as f:
                f.write(env_text)
        if deps == "upload":
            os.makedirs(self.p("_incoming", "_deps", repo, lockhash, "node_modules", "left-pad"), exist_ok=True)
        elif deps == "present":
            os.makedirs(self.p("_deps", repo, lockhash, "node_modules", "left-pad"), exist_ok=True)

    def deploy(self, repo, kind, name, slug=SLUG, branch=BRANCH, sha="abc1234", extra=()):
        return ["deploy", "--slug", slug, "--branch", branch, "--repo", repo,
                "--kind", kind, "--name", name, "--sha", sha, *extra]

    def deploy_fe(self, repo="so360-crm-fe", name="crm", slug=SLUG, **kw):
        self.upload_fe(repo, slug=slug)
        return self.ok(*self.deploy(repo, "fe", name, slug=slug, **kw))

    def deploy_be(self, repo="so360-crm-be", name="crm", slug=SLUG, lockhash=HASH, **kw):
        self.upload_be(repo, slug=slug, lockhash=lockhash)
        return self.ok(*self.deploy(repo, "be", name, slug=slug, extra=("--lockhash", lockhash)))

    def upload_site(self, repo, slug=SLUG):
        d = self.p("_incoming", slug, repo)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w") as f:
            f.write("<html></html>")

    def upload_next(self, repo, slug=SLUG, standalone=False, env_text="NEXT_PUBLIC_API_URL=https://dev-api.skyoffice360.com\n",
                    deps="upload", lockhash=HASH):
        d = self.p("_incoming", slug, repo)
        os.makedirs(os.path.join(d, ".next", "static"), exist_ok=True)
        os.makedirs(os.path.join(d, "public"), exist_ok=True)
        if env_text is not None:
            with open(os.path.join(d, ".env.local"), "w") as f:
                f.write(env_text)
        if standalone:
            os.makedirs(os.path.join(d, ".next", "standalone"), exist_ok=True)
            with open(os.path.join(d, ".next", "standalone", "server.js"), "w") as f:
                f.write("// server\n")
        elif deps == "upload":
            nb = self.p("_incoming", "_deps", repo, lockhash, "node_modules", "next", "dist", "bin")
            os.makedirs(nb, exist_ok=True)
            with open(os.path.join(nb, "next"), "w") as f:
                f.write("// next\n")

    def site_host(self, name, slug=SLUG):
        return f"pr-{slug}--{name}.skyoffice360.com"

    def deploy_site(self, repo="so360-sso-fe", name="sso", slug=SLUG, dev_host="dev-sso.skyoffice360.com"):
        self.upload_site(repo, slug=slug)
        extra = ("--host", self.site_host(name, slug)) + (("--dev-host", dev_host) if dev_host else ())
        return self.ok(*self.deploy(repo, "site", name, slug=slug, extra=extra))

    def deploy_next(self, repo="so360-command", name="command", slug=SLUG, standalone=False, **kw):
        self.upload_next(repo, slug=slug, standalone=standalone, **kw)
        extra = ("--host", self.site_host(name, slug), "--dev-host", f"dev-{name}.skyoffice360.com")
        if not standalone:
            extra += ("--lockhash", HASH)
        return self.ok(*self.deploy(repo, "next", name, slug=slug, extra=extra))


# ── Dispatch & validation ────────────────────────────────────────────────────
class GivenAnyInvocation(PreviewCtlCase):
    def test_when_no_command_is_given_then_usage_exits_2(self):
        r = self.ctl()
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage: preview-ctl", r.stderr)

    def test_when_an_unknown_command_is_given_then_usage_exits_2(self):
        self.assertEqual(self.ctl("destroy-everything").returncode, 2)

    def test_when_list_runs_on_an_empty_host_then_it_prints_an_empty_registry_without_locking(self):
        r = self.ok("list")
        self.assertEqual(json.loads(r.stdout), {"previews": {}})
        self.assertFalse(os.path.exists(self.p(".lock")))

    def test_when_list_runs_then_it_prints_the_registry_verbatim(self):
        self.write_registry({"previews": {"a": {"branch": "feat/a", "components": {}}}})
        self.assertEqual(json.loads(self.ok("list").stdout)["previews"]["a"]["branch"], "feat/a")

    def test_when_a_mutating_command_runs_then_it_takes_the_preview_lock(self):
        self.ok("render")
        self.assertTrue(os.path.exists(self.p(".lock")))
        self.assertIn("flock -w 600 9", self.calls())


class GivenInvalidDeployArguments(PreviewCtlCase):
    def test_when_the_slug_is_invalid_then_deploy_is_refused(self):
        for slug in ("Bad_Slug", "-lead", "trail-", "a" * 51, "../etc", ""):
            with self.subTest(slug=slug):
                self.fails(*self.deploy("so360-crm-fe", "fe", "crm", slug=slug), message="invalid slug")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "etc")))

    def test_when_the_repo_is_invalid_then_deploy_is_refused(self):
        self.fails(*self.deploy("org/so360-crm-fe", "fe", "crm"), message="invalid repo")

    def test_when_the_name_is_invalid_then_deploy_is_refused(self):
        for name in ("CRM", "-crm", "crm/../x", "a" * 42):
            with self.subTest(name=name):
                self.fails(*self.deploy("so360-crm-fe", "fe", name), message="invalid name")

    def test_when_an_unknown_flag_is_passed_then_deploy_is_refused(self):
        self.fails(*self.deploy("so360-crm-fe", "fe", "crm", extra=("--force", "1")), message="deploy: unknown flag --force")

    def test_when_branch_or_sha_is_missing_then_deploy_is_refused(self):
        self.fails(*self.deploy("so360-crm-fe", "fe", "crm", branch=""), message="--branch and --sha are required")
        self.fails(*self.deploy("so360-crm-fe", "fe", "crm", sha=""), message="--branch and --sha are required")

    def test_when_the_health_path_is_unsafe_then_deploy_is_refused(self):
        for health in ("health", "/health;rm -rf /", "/h?x=1"):
            with self.subTest(health=health):
                self.fails(*self.deploy("so360-crm-fe", "fe", "crm", extra=("--health", health)),
                           message="invalid health path")

    def test_when_the_kind_is_unknown_then_deploy_is_refused(self):
        self.upload_fe("so360-crm-fe")
        self.fails(*self.deploy("so360-crm-fe", "db", "crm"), message="--kind must be fe, shell, be, site or next")

    def test_when_nothing_was_uploaded_then_deploy_is_refused(self):
        self.fails(*self.deploy("so360-crm-fe", "fe", "crm"), message="nothing uploaded at")
        self.assertFalse(os.path.exists(self.p("registry.json")))


# ── Frontend / shell deploys ─────────────────────────────────────────────────
class GivenAnMfeUpload(PreviewCtlCase):
    def test_when_deployed_then_the_build_is_swapped_in_and_the_url_printed(self):
        r = self.deploy_fe()
        self.assertIn(f"PREVIEW_URL=https://pr-{SLUG}.skyoffice360.com", r.stdout)
        self.assertTrue(os.path.isfile(self.p(SLUG, "cdn", "crm", "remoteEntry.js")))
        self.assertFalse(os.path.exists(self.p("_incoming", SLUG, "so360-crm-fe")))

    def test_when_deployed_then_the_registry_records_the_component(self):
        self.deploy_fe(sha="deadbee")
        pv = self.registry()["previews"][SLUG]
        self.assertEqual(pv["branch"], BRANCH)
        self.assertFalse(pv["asleep"])
        comp = pv["components"]["so360-crm-fe"]
        self.assertEqual((comp["kind"], comp["name"], comp["sha"]), ("fe", "crm", "deadbee"))
        self.assertIsNone(comp["port"])
        self.assertIsNone(comp["lockhash"])
        self.assertEqual(comp["health"], "/health")

    def test_when_deployed_then_nginx_serves_the_mfe_from_disk_and_falls_back_for_the_shell(self):
        self.deploy_fe()
        conf = self.nginx()
        self.assertIn(f"server_name pr-{SLUG}.skyoffice360.com;", conf)
        self.assertIn("location ^~ /cdn/crm/ {", conf)
        self.assertIn(f"alias {self.p(SLUG, 'cdn', 'crm')}/;", conf)
        self.assertIn("set $pv_upstream_host dev-dashboard.skyoffice360.com;", conf)
        self.assertIn("include /etc/nginx/snippets/so360-preview-fallback.conf;", conf)
        self.assertNotIn("location ^~ /api/", conf)

    def test_when_redeployed_then_the_old_build_is_replaced_atomically(self):
        self.deploy_fe()
        with open(self.p(SLUG, "cdn", "crm", "stale.js"), "w") as f:
            f.write("old")
        self.upload_fe("so360-crm-fe", content="v2")
        self.ok(*self.deploy("so360-crm-fe", "fe", "crm"))
        with open(self.p(SLUG, "cdn", "crm", "remoteEntry.js")) as f:
            self.assertEqual(f.read(), "v2")
        self.assertFalse(os.path.exists(self.p(SLUG, "cdn", "crm", "stale.js")))
        self.assertFalse(os.path.exists(self.p(SLUG, "cdn", "crm.old")))

    def test_when_redeployed_then_created_is_kept_and_last_push_advances(self):
        self.deploy_fe()
        reg = self.registry()
        reg["previews"][SLUG]["created"] = 1000
        reg["previews"][SLUG]["last_push"] = 1000
        self.write_registry(reg)
        self.upload_fe("so360-crm-fe")
        self.ok(*self.deploy("so360-crm-fe", "fe", "crm"))
        pv = self.registry()["previews"][SLUG]
        self.assertEqual(pv["created"], 1000)
        self.assertGreater(pv["last_push"], 1000)

    def test_when_deployed_then_nginx_is_tested_and_reloaded(self):
        self.mark("nginx_active")
        self.deploy_fe()
        self.assertIn("nginx -t", self.calls())
        self.assertIn("systemctl reload nginx", self.calls())


class GivenAShellUpload(PreviewCtlCase):
    def test_when_deployed_then_nginx_serves_the_preview_shell_instead_of_develop(self):
        self.upload_fe("so360-shell-fe")
        self.ok(*self.deploy("so360-shell-fe", "shell", "shell"))
        self.assertTrue(os.path.isfile(self.p(SLUG, "shell", "remoteEntry.js")))
        conf = self.nginx()
        self.assertIn(f"root {self.p(SLUG, 'shell')};", conf)
        self.assertIn("try_files $uri $uri/ /index.html =404;", conf)
        self.assertNotIn("so360-preview-fallback.conf", conf)


# ── Backend deploys ──────────────────────────────────────────────────────────
class GivenABackendUpload(PreviewCtlCase):
    def test_when_deps_are_uploaded_then_they_move_into_the_shared_store_and_are_symlinked(self):
        self.deploy_be()
        dep = self.p("_deps", "so360-crm-be", HASH, "node_modules")
        self.assertTrue(os.path.isdir(dep))
        self.assertFalse(os.path.exists(self.p("_incoming", "_deps", "so360-crm-be", HASH)))
        link = self.p(SLUG, "be", "crm", "node_modules")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), dep)

    def test_when_deps_are_already_present_then_no_upload_is_needed(self):
        self.upload_be("so360-crm-be", deps="present")
        self.ok(*self.deploy("so360-crm-be", "be", "crm", extra=("--lockhash", HASH)))
        self.assertTrue(os.path.islink(self.p(SLUG, "be", "crm", "node_modules")))

    def test_when_deps_are_neither_present_nor_uploaded_then_deploy_is_refused(self):
        self.upload_be("so360-crm-be", deps=None)
        self.fails(*self.deploy("so360-crm-be", "be", "crm", extra=("--lockhash", HASH)),
                   message="neither present nor uploaded")

    def test_when_the_lockhash_is_missing_or_invalid_then_deploy_is_refused(self):
        self.upload_be("so360-crm-be")
        self.fails(*self.deploy("so360-crm-be", "be", "crm"), message="invalid lockhash ''")
        self.fails(*self.deploy("so360-crm-be", "be", "crm", extra=("--lockhash", "../../x")), message="invalid lockhash")

    def test_when_deployed_then_it_starts_under_pm2_on_the_first_free_port_and_saves(self):
        r = self.deploy_be()
        self.assertIn("health ok: pv-feat-crm-owner-crm on :7100", r.stdout)
        start = [c for c in self.calls() if c.startswith("pm2 start")]
        self.assertEqual(len(start), 1)
        self.assertIn("--name pv-feat-crm-owner-crm", start[0])
        self.assertIn("--max-memory-restart 300M", start[0])
        self.assertIn("--instances 1", start[0])
        self.assertTrue(start[0].endswith("PORT=7100"))
        self.assertIn("curl -fsS -o /dev/null http://127.0.0.1:7100/health", self.calls())
        self.assertIn("pm2 save PORT=", self.calls())
        comp = self.registry()["previews"][SLUG]["components"]["so360-crm-be"]
        self.assertEqual((comp["port"], comp["lockhash"]), (7100, HASH))

    def test_when_deployed_then_nginx_proxies_the_api_to_the_pm2_port(self):
        self.deploy_be()
        conf = self.nginx()
        self.assertIn("location ^~ /api/crm/ {", conf)
        self.assertIn("proxy_pass http://127.0.0.1:7100;", conf)
        self.assertIn("error_page 502 503 504 = @asleep;", conf)

    def test_when_a_custom_health_path_is_given_then_it_is_probed(self):
        self.upload_be("so360-crm-be")
        self.ok(*self.deploy("so360-crm-be", "be", "crm", extra=("--lockhash", HASH, "--health", "/v1/health")))
        self.assertIn("curl -fsS -o /dev/null http://127.0.0.1:7100/v1/health", self.calls())

    def test_when_redeployed_then_the_same_port_is_reused(self):
        self.deploy_be()
        self.deploy_be()
        self.assertEqual(self.registry()["previews"][SLUG]["components"]["so360-crm-be"]["port"], 7100)

    def test_when_ports_are_registered_or_busy_then_they_are_skipped(self):
        self.deploy_be("so360-crm-be", "crm")
        self.mark("busy_ports", "7101\n")
        self.deploy_be("so360-inventory-be", "inventory", slug="feat-other")
        self.assertEqual(self.registry()["previews"]["feat-other"]["components"]["so360-inventory-be"]["port"], 7102)

    def test_when_every_port_is_taken_then_deploy_is_refused(self):
        self.mark("busy_ports", "\n".join(str(p) for p in range(7100, 7106)) + "\n")
        self.upload_be("so360-crm-be")
        self.fails(*self.deploy("so360-crm-be", "be", "crm", extra=("--lockhash", HASH)),
                   message="no free preview port in 7100-7105")

    def test_when_the_upload_has_no_env_then_deploy_is_refused(self):
        self.upload_be("so360-crm-be", env_file=False)
        self.fails(*self.deploy("so360-crm-be", "be", "crm", extra=("--lockhash", HASH)),
                   message="backend upload has no .env")

    def test_when_the_build_has_no_main_js_then_deploy_is_refused(self):
        self.upload_be("so360-crm-be", main=False)
        self.fails(*self.deploy("so360-crm-be", "be", "crm", extra=("--lockhash", HASH)), message="no dist/**/main.js")

    def test_when_the_backend_never_becomes_healthy_then_it_is_stopped_and_deploy_fails(self):
        self.mark("curl_fail")
        self.upload_be("so360-crm-be")
        self.fails(*self.deploy("so360-crm-be", "be", "crm", extra=("--lockhash", HASH)),
                   message="so360-crm-be did not become healthy on :7100")
        calls = self.calls()
        self.assertEqual(sum(c.startswith("curl ") for c in calls), 30)
        self.assertIn("pm2 logs pv-feat-crm-owner-crm --lines 60 --nostream PORT=", calls)
        self.assertIn("pm2 stop pv-feat-crm-owner-crm PORT=", calls)
        self.assertNotIn("pm2 save PORT=", calls)


class GivenABackendEnv(PreviewCtlCase):
    def env_after_deploy(self, env_text):
        self.upload_be("so360-crm-be", env_text=env_text)
        self.ok(*self.deploy("so360-crm-be", "be", "crm", extra=("--lockhash", HASH)))
        path = self.p(SLUG, "be", "crm", ".env")
        with open(path) as f:
            return f.read().splitlines(), stat.S_IMODE(os.stat(path).st_mode)

    def test_when_deployed_then_preview_flags_are_forced_and_workers_disabled(self):
        lines, _ = self.env_after_deploy("SUPABASE_URL=https://preview-db.supabase.co\n")
        for flag in ("PORT=7100", "SO360_PREVIEW=true", "EVENT_WORKERS_DISABLED=true",
                     "SIGNAL_EVENT_CONSUMER_DISABLED=true", "SIGNAL_RULES_SWEEP=off"):
            self.assertIn(flag, lines)
        self.assertIn("SUPABASE_URL=https://preview-db.supabase.co", lines)

    def test_when_the_upload_tries_to_override_the_guard_flags_then_they_are_replaced_not_duplicated(self):
        lines, _ = self.env_after_deploy(
            "PORT=3005\nSO360_PREVIEW=false\nEVENT_WORKERS_DISABLED=false\n"
            "SIGNAL_EVENT_CONSUMER_DISABLED=false\nSIGNAL_RULES_SWEEP=on\nKEEP=1\n")
        for key in ("PORT", "SO360_PREVIEW", "EVENT_WORKERS_DISABLED", "SIGNAL_EVENT_CONSUMER_DISABLED", "SIGNAL_RULES_SWEEP"):
            self.assertEqual(sum(l.startswith(key + "=") for l in lines), 1, key)
        self.assertNotIn("SO360_PREVIEW=false", lines)
        self.assertNotIn("PORT=3005", lines)
        self.assertIn("KEEP=1", lines)

    def test_when_deployed_then_the_env_is_private(self):
        _, mode = self.env_after_deploy("A=1\n")
        self.assertEqual(mode, 0o600)

    def test_when_the_env_points_at_the_production_database_then_deploy_is_refused_before_start(self):
        self.upload_be("so360-crm-be", env_text="SUPABASE_URL=https://prodref.supabase.co\n")
        self.fails(*self.deploy("so360-crm-be", "be", "crm", extra=("--lockhash", HASH)),
                   message="references the production database",
                   env={"PROD_SUPABASE_HOST": "prodref.supabase.co"})
        self.assertFalse(any(c.startswith("pm2 start") for c in self.calls()))

    def test_when_ci_env_carries_the_production_database_host_then_deploy_is_refused(self):
        path = self.env["PREVIEW_CI_ENV_FILE"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("PROD_SUPABASE_HOST=prodref.supabase.co\n")
        self.upload_be("so360-crm-be", env_text="SUPABASE_URL=https://prodref.supabase.co\n")
        self.fails(*self.deploy("so360-crm-be", "be", "crm", extra=("--lockhash", HASH)),
                   message="references the production database")
        self.assertFalse(any(c.startswith("pm2 start") for c in self.calls()))


# ── Sleep / wake ─────────────────────────────────────────────────────────────
class GivenARunningPreview(PreviewCtlCase):
    def setUp(self):
        super().setUp()
        self.deploy_be()
        self.clear_calls()

    def test_when_slept_then_backends_stop_and_the_preview_is_marked_asleep(self):
        self.ok("sleep", "--slug", SLUG)
        self.assertIn("pm2 stop pv-feat-crm-owner-crm PORT=", self.calls())
        self.assertTrue(self.registry()["previews"][SLUG]["asleep"])
        self.assertTrue(os.path.isdir(self.p(SLUG, "be", "crm")))

    def test_when_woken_then_stopped_backends_restart_and_asleep_clears(self):
        self.ok("sleep", "--slug", SLUG)
        self.clear_calls()
        self.ok("wake", "--slug", SLUG)
        self.assertTrue(any(c.startswith("pm2 start") for c in self.calls()))
        self.assertFalse(self.registry()["previews"][SLUG]["asleep"])

    def test_when_woken_while_already_online_then_nothing_restarts(self):
        self.ok("wake", "--slug", SLUG)
        self.assertFalse(any(c.startswith("pm2 start") for c in self.calls()))

    def test_when_a_backend_fails_to_wake_then_it_warns_and_does_not_fail(self):
        self.ok("sleep", "--slug", SLUG)
        self.mark("curl_fail")
        r = self.ok("wake", "--slug", SLUG)
        self.assertIn("::warning::pv-feat-crm-owner-crm failed to wake", r.stdout)
        self.assertFalse(self.registry()["previews"][SLUG]["asleep"])

    def test_when_another_component_is_pushed_then_the_whole_preview_wakes(self):
        self.ok("sleep", "--slug", SLUG)
        self.clear_calls()
        self.deploy_fe()
        self.assertTrue(any(c.startswith("pm2 start") and "pv-feat-crm-owner-crm" in c for c in self.calls()))
        self.assertFalse(self.registry()["previews"][SLUG]["asleep"])

    def test_when_sleep_or_wake_lack_the_slug_flag_then_they_are_refused(self):
        self.fails("sleep", message="usage: sleep --slug S")
        self.fails("wake", SLUG, message="usage: wake --slug S")
        self.fails("sleep", "--slug", "BAD", message="invalid slug")


class GivenAnUnknownPreview(PreviewCtlCase):
    def test_when_slept_then_no_registry_entry_is_invented(self):
        self.ok("sleep", "--slug", "feat-nothing")
        self.assertEqual(self.registry(), {"previews": {}})

    def test_when_woken_then_no_registry_entry_is_invented(self):
        self.ok("wake", "--slug", "feat-nothing")
        self.assertEqual(self.registry(), {"previews": {}})


# ── Standalone apps: SPA fe, site, next ──────────────────────────────────────
class GivenAStandaloneSpaUnderCdn(PreviewCtlCase):
    def test_when_deployed_with_spa_then_deep_links_fall_back_to_its_index(self):
        self.upload_fe("so360-help-fe")
        self.ok(*self.deploy("so360-help-fe", "fe", "contact-support", extra=("--spa",)))
        comp = self.registry()["previews"][SLUG]["components"]["so360-help-fe"]
        self.assertTrue(comp["spa"])
        self.assertIn("try_files $uri /cdn/contact-support/index.html;", self.nginx())

    def test_when_deployed_without_spa_then_the_mfe_has_no_fallback_and_no_spa_flag(self):
        self.deploy_fe()
        self.assertNotIn("spa", self.registry()["previews"][SLUG]["components"]["so360-crm-fe"])
        self.assertIn("try_files $uri =404;", self.nginx())

    def test_when_spa_is_passed_for_another_kind_then_deploy_is_refused(self):
        self.upload_be("so360-crm-be")
        self.fails(*self.deploy("so360-crm-be", "be", "crm", extra=("--lockhash", HASH, "--spa")),
                   message="--spa only applies to --kind fe")


class GivenAStaticSite(PreviewCtlCase):
    def test_when_deployed_then_it_gets_its_own_host_with_the_overlay(self):
        r = self.deploy_site()
        host = self.site_host("sso")
        comp = self.registry()["previews"][SLUG]["components"]["so360-sso-fe"]
        self.assertEqual((comp["kind"], comp["host"], comp["dev_host"]), ("site", host, "dev-sso.skyoffice360.com"))
        self.assertTrue(os.path.isfile(self.p(SLUG, "site", "sso", "index.html")))
        conf = self.nginx()
        self.assertIn(f"server_name {host};", conf)
        self.assertIn(f"root {self.p(SLUG, 'site', 'sso')};", conf)
        self.assertIn(f"set $pv_api_base {host}/api;", conf)
        # Both hosts rewrite the app's dev hostname, so the shell links to it.
        self.assertEqual(conf.count(f"sub_filter 'https://dev-sso.skyoffice360.com' 'https://{host}';"), 2)
        self.assertEqual(conf.count("include /etc/nginx/snippets/so360-preview-overlay.conf;"), 2)
        self.assertIn(f"PREVIEW_HOST_URL=https://{host}", r.stdout)

    def test_when_the_host_is_missing_or_malformed_then_deploy_is_refused(self):
        for host in ("", "sso.skyoffice360.com", "pr-x.example.com", "pr-x-.skyoffice360.com",
                     "pr-" + "a" * 61 + ".skyoffice360.com"):
            with self.subTest(host=host):
                self.upload_site("so360-sso-fe")
                self.fails(*self.deploy("so360-sso-fe", "site", "sso", extra=("--host", host)),
                           message="invalid host")

    def test_when_the_host_is_the_main_preview_host_then_deploy_is_refused(self):
        self.upload_site("so360-sso-fe")
        self.fails(*self.deploy("so360-sso-fe", "site", "sso", extra=("--host", f"pr-{SLUG}.skyoffice360.com")),
                   message="--host must differ")

    def test_when_the_host_belongs_to_another_component_then_deploy_is_refused(self):
        self.deploy_site()
        self.upload_site("so360-portal")
        self.fails(*self.deploy("so360-portal", "site", "portal", extra=("--host", self.site_host("sso"))),
                   message="already belongs to")

    def test_when_the_dev_host_is_one_the_overlay_rewrites_then_deploy_is_refused(self):
        self.upload_site("so360-sso-fe")
        self.fails(*self.deploy("so360-sso-fe", "site", "sso",
                                extra=("--host", self.site_host("sso"), "--dev-host", "dev-api.skyoffice360.com")),
                   message="already rewritten by the overlay")

    def test_when_a_host_is_passed_for_an_mfe_then_deploy_is_refused(self):
        self.upload_fe("so360-crm-fe")
        self.fails(*self.deploy("so360-crm-fe", "fe", "crm", extra=("--host", self.site_host("crm"))),
                   message="--host/--dev-host only apply to --kind site or next")

    def test_when_no_next_app_exists_then_no_loopback_listeners_are_rendered(self):
        self.deploy_site()
        self.assertNotIn("listen 127.0.0.1:", self.nginx())


class GivenANextApp(PreviewCtlCase):
    def test_when_deployed_then_it_runs_under_pm2_with_shared_deps_and_is_probed_at_root(self):
        r = self.deploy_next()
        comp = self.registry()["previews"][SLUG]["components"]["so360-command"]
        self.assertEqual((comp["kind"], comp["port"], comp["health"], comp["lockhash"]), ("next", 7100, "/", HASH))
        self.assertTrue(os.path.islink(self.p(SLUG, "next", "command", "node_modules")))
        starts = [c for c in self.calls() if c.startswith("pm2 start ")]
        self.assertEqual(len(starts), 1)
        self.assertIn("node_modules/next/dist/bin/next --name pv-feat-crm-owner-command", starts[0])
        self.assertIn("-- start -p 7100 -H 127.0.0.1", starts[0])
        self.assertIn("curl -fsS -o /dev/null http://127.0.0.1:7100/", self.calls())
        self.assertIn("health ok: pv-feat-crm-owner-command on :7100", r.stdout)

    def test_when_deployed_then_nginx_proxies_its_host_and_reaches_develop_via_so360api(self):
        self.deploy_next()
        host = self.site_host("command")
        conf = self.nginx()
        self.assertIn(f"server_name {host};", conf)
        self.assertIn("proxy_pass http://127.0.0.1:7100;", conf)
        self.assertIn("location ^~ /__so360api/ {", conf)
        self.assertIn(f"set $pv_api_base {host}/__so360api;", conf)
        self.assertIn(f"set $pv_cdn_base pr-{SLUG}.skyoffice360.com/cdn;", conf)
        # Server-side calls to develop's service ports land on loopback.
        self.assertIn("listen 127.0.0.1:6003;", conf)
        self.assertIn("proxy_pass https://so360_dev_origin/crm/;", conf)

    def test_when_deployed_then_service_keys_are_stripped_and_the_preview_flag_set(self):
        self.deploy_next(env_text="SUPABASE_SERVICE_ROLE_KEY=secret\nNEXT_SUPABASE_SERVICE_KEY=secret\n"
                                  "PORT=3000\nHOSTNAME=0.0.0.0\nSO360_PREVIEW=false\nNEXT_PUBLIC_X=1\n")
        path = self.p(SLUG, "next", "command", ".env.local")
        with open(path) as f:
            env = f.read()
        self.assertNotIn("SERVICE", env)
        self.assertNotIn("PORT=", env)
        self.assertNotIn("HOSTNAME=", env)
        self.assertIn("NEXT_PUBLIC_X=1", env)
        self.assertEqual(env.count("SO360_PREVIEW="), 1)
        self.assertIn("SO360_PREVIEW=true", env)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_when_the_upload_has_no_env_local_then_deploy_is_refused(self):
        self.upload_next("so360-command", env_text=None)
        self.fails(*self.deploy("so360-command", "next", "command",
                                extra=("--host", self.site_host("command"), "--lockhash", HASH)),
                   message="next upload has no .env.local")

    def test_when_the_build_is_standalone_then_its_server_runs_with_static_beside_it(self):
        self.deploy_next(standalone=True)
        comp = self.registry()["previews"][SLUG]["components"]["so360-command"]
        self.assertIsNone(comp["lockhash"])
        sa = self.p(SLUG, "next", "command", ".next", "standalone")
        self.assertTrue(os.path.isdir(os.path.join(sa, ".next", "static")))
        self.assertTrue(os.path.isdir(os.path.join(sa, "public")))
        self.assertTrue(os.path.isfile(os.path.join(sa, ".env.local")))
        starts = [c for c in self.calls() if c.startswith("pm2 start ")]
        self.assertIn(".next/standalone/server.js --name pv-feat-crm-owner-command", starts[0])
        self.assertIn("PORT=7100", starts[0])

    def test_when_slept_and_woken_then_the_next_app_stops_and_restarts(self):
        self.deploy_next()
        self.clear_calls()
        self.ok("sleep", "--slug", SLUG)
        self.assertIn("pm2 stop pv-feat-crm-owner-command PORT=", self.calls())
        self.clear_calls()
        self.ok("wake", "--slug", SLUG)
        self.assertTrue(any(c.startswith("pm2 start ") and "pv-feat-crm-owner-command" in c for c in self.calls()))


class GivenAPreviewWithExtraHosts(PreviewCtlCase):
    def setUp(self):
        super().setUp()
        self.deploy_fe()
        self.deploy_site()
        self.deploy_next()
        self.clear_calls()

    def test_when_the_site_is_removed_then_its_host_is_reported_and_the_preview_stays(self):
        r = self.ok("remove", "--slug", SLUG, "--repo", "so360-sso-fe")
        lines = r.stdout.strip().splitlines()
        self.assertIn(f"REMOVED_HOST={self.site_host('sso')}", lines)
        self.assertEqual(lines[-1], "REMOVED_PREVIEW=0")
        self.assertFalse(os.path.exists(self.p(SLUG, "site", "sso")))
        self.assertNotIn(f"server_name {self.site_host('sso')};", self.nginx())

    def test_when_the_next_app_is_removed_then_its_process_goes_and_loopback_is_dropped(self):
        r = self.ok("remove", "--slug", SLUG, "--repo", "so360-command")
        self.assertIn(f"REMOVED_HOST={self.site_host('command')}", r.stdout)
        self.assertIn("pm2 delete pv-feat-crm-owner-command PORT=", self.calls())
        self.assertFalse(os.path.exists(self.p(SLUG, "next", "command")))
        self.assertNotIn("listen 127.0.0.1:", self.nginx())

    def test_when_an_mfe_is_removed_then_no_host_is_reported(self):
        r = self.ok("remove", "--slug", SLUG, "--repo", "so360-crm-fe")
        self.assertNotIn("REMOVED_HOST=", r.stdout)

    def test_when_the_whole_preview_is_removed_then_every_extra_host_is_reported(self):
        r = self.ok("remove", "--slug", SLUG)
        lines = r.stdout.strip().splitlines()
        self.assertIn(f"REMOVED_HOST={self.site_host('sso')}", lines)
        self.assertIn(f"REMOVED_HOST={self.site_host('command')}", lines)
        self.assertEqual(lines[-1], "REMOVED_PREVIEW=1")
        self.assertIn("pm2 delete pv-feat-crm-owner-command PORT=", self.calls())


# ── Remove ───────────────────────────────────────────────────────────────────
class GivenAPreviewWithSeveralComponents(PreviewCtlCase):
    def setUp(self):
        super().setUp()
        self.deploy_fe("so360-crm-fe", "crm")
        self.deploy_fe("so360-inventory-fe", "inventory")
        self.deploy_be("so360-crm-be", "crm")
        self.clear_calls()

    def test_when_one_mfe_is_removed_then_the_preview_stays(self):
        r = self.ok("remove", "--slug", SLUG, "--repo", "so360-crm-fe")
        self.assertEqual(r.stdout.strip().splitlines()[-1], "REMOVED_PREVIEW=0")
        self.assertFalse(os.path.exists(self.p(SLUG, "cdn", "crm")))
        self.assertTrue(os.path.isdir(self.p(SLUG, "cdn", "inventory")))
        comps = self.registry()["previews"][SLUG]["components"]
        self.assertNotIn("so360-crm-fe", comps)
        self.assertNotIn("location ^~ /cdn/crm/", self.nginx())
        self.assertIn("location ^~ /cdn/inventory/", self.nginx())

    def test_when_the_backend_is_removed_then_its_pm2_process_is_deleted(self):
        r = self.ok("remove", "--slug", SLUG, "--repo", "so360-crm-be")
        self.assertIn("REMOVED_PREVIEW=0", r.stdout)
        self.assertIn("pm2 delete pv-feat-crm-owner-crm PORT=", self.calls())
        self.assertFalse(os.path.exists(self.p(SLUG, "be", "crm")))
        self.assertNotIn("location ^~ /api/crm/", self.nginx())
        # Shared deps survive: other previews may use them.
        self.assertTrue(os.path.isdir(self.p("_deps", "so360-crm-be", HASH, "node_modules")))

    def test_when_the_last_component_is_removed_then_the_whole_preview_goes(self):
        for repo in ("so360-crm-fe", "so360-inventory-fe"):
            self.ok("remove", "--slug", SLUG, "--repo", repo)
        r = self.ok("remove", "--slug", SLUG, "--repo", "so360-crm-be")
        self.assertEqual(r.stdout.strip().splitlines()[-1], "REMOVED_PREVIEW=1")
        self.assertFalse(os.path.exists(self.p(SLUG)))
        self.assertNotIn(SLUG, self.registry()["previews"])
        self.assertNotIn(f"pr-{SLUG}.skyoffice360.com;", self.nginx())

    def test_when_the_whole_preview_is_removed_then_every_backend_and_upload_is_cleaned(self):
        os.makedirs(self.p("_incoming", SLUG, "half-uploaded"))
        r = self.ok("remove", "--slug", SLUG)
        self.assertIn("REMOVED_PREVIEW=1", r.stdout)
        self.assertIn("pm2 delete pv-feat-crm-owner-crm PORT=", self.calls())
        self.assertIn("pm2 save PORT=", self.calls())
        self.assertFalse(os.path.exists(self.p(SLUG)))
        self.assertFalse(os.path.exists(self.p("_incoming", SLUG)))
        self.assertEqual(self.registry()["previews"], {})

    def test_when_another_preview_exists_then_removing_this_one_leaves_it_alone(self):
        self.deploy_fe(slug="feat-other")
        self.ok("remove", "--slug", SLUG)
        self.assertIn("feat-other", self.registry()["previews"])
        self.assertTrue(os.path.isdir(self.p("feat-other", "cdn", "crm")))
        self.assertIn("server_name pr-feat-other.skyoffice360.com;", self.nginx())

    def test_when_remove_gets_bad_arguments_then_it_is_refused(self):
        self.fails("remove", "--slug", SLUG, "--all", message="remove: unknown flag --all")
        self.fails("remove", "--slug", "../x", message="invalid slug")
        self.fails("remove", "--slug", SLUG, "--repo", "a/b", message="invalid repo")


class GivenNoSuchPreview(PreviewCtlCase):
    def test_when_removed_then_it_is_an_idempotent_no_op(self):
        r = self.ok("remove", "--slug", "feat-gone", "--repo", "so360-crm-fe")
        self.assertIn("REMOVED_PREVIEW=1", r.stdout)
        self.assertEqual(self.registry(), {"previews": {}})


# ── Shared dependencies ──────────────────────────────────────────────────────
class GivenSharedDeps(PreviewCtlCase):
    def test_when_deps_exist_then_deps_present_succeeds_without_locking(self):
        os.makedirs(self.p("_deps", "so360-crm-be", HASH, "node_modules"))
        self.ok("deps-present", "--repo", "so360-crm-be", "--lockhash", HASH)
        self.assertFalse(os.path.exists(self.p(".lock")))

    def test_when_deps_are_missing_then_deps_present_fails_quietly(self):
        r = self.ctl("deps-present", "--repo", "so360-crm-be", "--lockhash", HASH)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr, "")

    def test_when_deps_present_gets_bad_arguments_then_it_is_refused(self):
        self.fails("deps-present", "--repo", "so360-crm-be", "--lockhash", "XYZ", message="invalid lockhash")
        self.fails("deps-present", "--repo", "a/b", "--lockhash", HASH, message="invalid repo")
        self.fails("deps-present", "--nope", "1", message="deps-present: unknown flag --nope")

    def test_when_pruned_then_only_old_unreferenced_deps_are_removed(self):
        old = time.time() - 3 * 86400
        dirs = {
            "referenced_old": self.p("_deps", "so360-crm-be", "aaaa1111"),
            "unreferenced_old": self.p("_deps", "so360-crm-be", "bbbb2222"),
            "unreferenced_fresh": self.p("_deps", "so360-inventory-be", "cccc3333"),
        }
        for d in dirs.values():
            os.makedirs(os.path.join(d, "node_modules"))
        for key in ("referenced_old", "unreferenced_old"):
            os.utime(dirs[key], (old, old))
        self.write_registry({"previews": {"s": {"components": {
            "so360-crm-be": {"kind": "be", "name": "crm", "lockhash": "aaaa1111"},
            "so360-crm-fe": {"kind": "fe", "name": "crm", "lockhash": None}}}}})
        r = self.ok("prune-deps")
        self.assertTrue(os.path.isdir(dirs["referenced_old"]))
        self.assertTrue(os.path.isdir(dirs["unreferenced_fresh"]))
        self.assertFalse(os.path.exists(dirs["unreferenced_old"]))
        self.assertEqual(r.stdout.strip(), f"pruning {dirs['unreferenced_old']}")

    def test_when_pruned_on_an_empty_store_then_nothing_happens(self):
        self.assertEqual(self.ok("prune-deps").stdout, "")


# ── nginx rendering ──────────────────────────────────────────────────────────
class GivenRender(PreviewCtlCase):
    def test_when_dev_origin_is_unset_then_render_is_refused(self):
        self.fails("render", message="DEV_ORIGIN is not set", env={"DEV_ORIGIN": None})
        self.assertFalse(os.path.exists(self.nginx_out))

    def write_host_env(self, key, body):
        path = self.env[key]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(body)

    def test_when_the_env_files_are_missing_then_the_refusal_names_both_configured_files(self):
        r = self.fails("render", message="DEV_ORIGIN is not set", env={"DEV_ORIGIN": None})
        self.assertIn(self.env["PREVIEW_CI_ENV_FILE"], r.stderr)
        self.assertIn(self.env["PREVIEW_ENV_FILE"], r.stderr)
        self.assertIn("PREVIEW_DEV_ORIGIN secret", r.stderr)
        self.assertNotIn("/etc/so360-preview", r.stderr)

    def test_when_only_the_host_env_file_sets_dev_origin_then_render_uses_it(self):
        self.write_host_env("PREVIEW_ENV_FILE", "DEV_ORIGIN=10.9.9.9\n")
        self.ok("render", env={"DEV_ORIGIN": None})
        self.assertIn("upstream so360_dev_origin { server 10.9.9.9:443; keepalive 16; }", self.nginx())

    def test_when_the_ci_env_file_sets_dev_origin_then_it_wins_over_the_host_env_file(self):
        self.write_host_env("PREVIEW_ENV_FILE", "DEV_ORIGIN=10.9.9.9\n")
        self.write_host_env("PREVIEW_CI_ENV_FILE", "DEV_ORIGIN=10.8.8.8\n")
        self.ok("render", env={"DEV_ORIGIN": None})
        conf = self.nginx()
        self.assertIn("server 10.8.8.8:443;", conf)
        self.assertNotIn("10.9.9.9", conf)

    def test_when_rendered_empty_then_http_redirects_and_unknown_previews_get_a_noindex_404(self):
        self.ok("render")
        conf = self.nginx()
        self.assertIn("upstream so360_dev_origin { server 10.0.0.1:443; keepalive 16; }", conf)
        self.assertIn("keys_zone=so360pv:20m", conf)
        self.assertIn("return 301 https://$host$request_uri;", conf)
        self.assertIn(r"server_name ~^pr-[a-z0-9-]+\.skyoffice360\.com$;", conf)
        self.assertIn('return 404 "No preview is deployed for this branch (yet).\\n";', conf)
        self.assertIn('X-Robots-Tag "noindex, nofollow"', conf)
        self.assertIn("ssl_certificate     /certs/preview.crt;", conf)
        self.assertEqual(stat.S_IMODE(os.stat(self.nginx_out).st_mode), 0o644)
        self.assertTrue(any(c.startswith("install -d -m 700") for c in self.calls()))

    def test_when_nginx_is_running_then_it_is_reloaded(self):
        self.mark("nginx_active")
        self.ok("render")
        self.assertIn("systemctl reload nginx", self.calls())
        self.assertNotIn("systemctl start nginx", self.calls())

    def test_when_nginx_is_stopped_then_it_is_started(self):
        self.ok("render")
        self.assertIn("systemctl start nginx", self.calls())
        self.assertNotIn("systemctl reload nginx", self.calls())

    def test_when_the_new_config_fails_nginx_t_then_the_previous_config_is_restored(self):
        with open(self.nginx_out, "w") as f:
            f.write("# previous good config\n")
        self.mark("nginx_fail")
        self.fails("render", message="previous config restored")
        with open(self.nginx_out) as f:
            self.assertEqual(f.read(), "# previous good config\n")
        self.assertFalse(any(c.startswith("systemctl reload") or c.startswith("systemctl start") for c in self.calls()))

    def test_when_the_first_config_fails_nginx_t_then_no_broken_file_is_left(self):
        self.mark("nginx_fail")
        self.fails("render", message="failed 'nginx -t'")
        self.assertFalse(os.path.exists(self.nginx_out))

    def test_when_a_deploy_renders_a_broken_config_then_the_deploy_fails(self):
        self.mark("nginx_fail")
        self.upload_fe("so360-crm-fe")
        self.fails(*self.deploy("so360-crm-fe", "fe", "crm"), message="previous config restored")

    def test_when_several_previews_exist_then_each_gets_its_own_server_block(self):
        self.deploy_fe(slug="feat-a", branch="feat/a")
        self.deploy_fe(slug="feat-b", branch="feat/b")
        conf = self.nginx()
        self.assertIn("# ── feat-a (feat/a) ──", conf)
        self.assertIn("# ── feat-b (feat/b) ──", conf)
        self.assertIn("set $pv_slug feat-a;", conf)
        self.assertIn("set $pv_slug feat-b;", conf)
        self.assertEqual(conf.count("include /etc/nginx/snippets/so360-preview-common.conf;"), 2)


# ── Registry integrity ───────────────────────────────────────────────────────
class GivenACorruptRegistry(PreviewCtlCase):
    def test_when_a_command_would_write_it_then_the_write_is_refused_and_the_file_kept(self):
        bad = '{"previews":null}'
        self.write_registry(bad)
        self.fails("sleep", "--slug", SLUG, message="refusing to write a malformed registry")
        with open(self.p("registry.json")) as f:
            self.assertEqual(f.read(), bad)
        self.assertEqual([n for n in os.listdir(self.root) if n.startswith(".registry.")], [])

    def test_when_the_registry_is_empty_then_it_is_treated_as_no_previews(self):
        self.write_registry("")
        self.deploy_fe()
        self.assertEqual(list(self.registry()["previews"]), [SLUG])


# ── Cloudflare DNS + Access (token only on the host) ─────────────────────────
MAIN_HOST = f"pr-{SLUG}.skyoffice360.com"
APP_HOST = f"pr-{SLUG}--sso.skyoffice360.com"
TOKEN = "stub-cf-token-123"


class GivenCloudflareOnTheHost(PreviewCtlCase):
    def setUp(self):
        super().setUp()
        path = self.env["PREVIEW_CF_ENV_FILE"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(f"CF_API_TOKEN={TOKEN}\n")

    def cf_calls(self):
        return [c for c in self.calls() if "https://cf.test/" in c]

    def ensure(self, *fqdns, ip="207.180.217.124"):
        args = ["cf-ensure", "--slug", SLUG, "--ip", ip]
        for f in fqdns:
            args += ["--fqdn", f]
        return self.ok(*args)

    def test_when_the_host_has_no_token_file_then_it_refuses_and_calls_nothing(self):
        os.remove(self.env["PREVIEW_CF_ENV_FILE"])
        self.fails("cf-ensure", "--slug", SLUG, "--ip", "1.2.3.4", "--fqdn", MAIN_HOST, message="run preview-reconcile once")
        self.assertEqual(self.cf_calls(), [])

    def test_when_ensured_then_the_access_app_is_created_before_the_dns_record(self):
        r = self.ensure(MAIN_HOST, APP_HOST)
        writes = [c for c in self.cf_calls() if " -X " in c]
        self.assertEqual(len(writes), 4)
        for i, host in ((0, MAIN_HOST), (2, APP_HOST)):
            self.assertIn("-X POST https://cf.test/accounts/", writes[i])
            self.assertIn(f'"domain":"{host}"', writes[i].replace(" ", ""))
            self.assertIn("-X POST https://cf.test/zones/Z1/dns_records", writes[i + 1])
            self.assertIn(f'"so360-preview {SLUG}"', writes[i + 1])
        self.assertIn(f"Created Access app for {APP_HOST}", r.stdout)

    def test_when_ensured_then_the_token_goes_on_stdin_never_on_a_command_line(self):
        self.ensure(MAIN_HOST)
        self.assertFalse(any(TOKEN in c for c in self.calls()))
        with open(os.path.join(self.state, "curl_config")) as f:
            self.assertIn(f'header = "Authorization: Bearer {TOKEN}"', f.read())

    def test_when_the_app_and_record_exist_then_only_the_record_is_updated(self):
        self.mark("cf_apps", json.dumps({"result": [{"id": "A1", "domain": MAIN_HOST, "name": f"SO360 Preview – {SLUG}"}]}))
        self.mark("cf_dns", json.dumps({"result": [{"id": "D1", "name": MAIN_HOST, "comment": f"so360-preview {SLUG}"}]}))
        self.ensure(MAIN_HOST)
        writes = [c for c in self.cf_calls() if " -X " in c]
        self.assertEqual(len(writes), 1)
        self.assertIn("-X PUT https://cf.test/zones/Z1/dns_records/D1", writes[0])

    def test_when_a_host_is_not_a_preview_host_then_nothing_is_touched(self):
        for bad in ("dev-api.skyoffice360.com", "pr-x.neonbee.app", "skyoffice360.com"):
            self.fails("cf-ensure", "--slug", SLUG, "--ip", "1.2.3.4", "--fqdn", bad, message="invalid host")
            self.fails("cf-remove", "--fqdn", bad, message="invalid host")
        self.assertEqual(self.cf_calls(), [])

    def test_when_the_ip_is_not_an_ipv4_address_then_it_refuses(self):
        self.fails("cf-ensure", "--slug", SLUG, "--ip", "evil;rm", "--fqdn", MAIN_HOST, message="invalid --ip")
        self.assertEqual(self.cf_calls(), [])

    def test_when_removed_then_only_preview_records_go_dns_before_access(self):
        self.mark("cf_dns", json.dumps({"result": [
            {"id": "D1", "name": MAIN_HOST, "comment": f"so360-preview {SLUG}"},
            {"id": "D2", "name": MAIN_HOST, "comment": "hand-made"}]}))
        self.mark("cf_apps", json.dumps({"result": [
            {"id": "A1", "domain": MAIN_HOST, "name": f"SO360 Preview – {SLUG}"},
            {"id": "A2", "domain": MAIN_HOST, "name": "Someone else"}]}))
        r = self.ok("cf-remove", "--fqdn", MAIN_HOST)
        deletes = [c for c in self.cf_calls() if "-X DELETE" in c]
        self.assertEqual(len(deletes), 2)
        self.assertTrue(deletes[0].endswith("/zones/Z1/dns_records/D1"))
        self.assertTrue(deletes[1].endswith("/access/apps/A1"))
        self.assertIn(f"Deleted DNS {MAIN_HOST}", r.stdout)


if __name__ == "__main__":
    unittest.main()
