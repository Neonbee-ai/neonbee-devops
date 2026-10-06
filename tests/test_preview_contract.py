"""
BDD specs — feature-branch previews must never touch dev, prod or real data.

A preview runs branch code nobody has reviewed. These specs lock in the guard
rails described in preview/README.md. Run: python3 -m unittest discover tests
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows")
DEPLOY = os.path.join(WF, "neonbee-deploy-preview.yml")
TEARDOWN = os.path.join(WF, "neonbee-preview-teardown.yml")
RECONCILE = os.path.join(WF, "preview-reconcile.yml")
CTL = os.path.join(ROOT, "preview", "bin", "preview-ctl")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def slug_block(text):
    m = re.search(r"# ── slug \(keep identical.*?\n(.*?\n\s*fi\n)", text, re.S)
    assert m, "slug block not found"
    return "\n".join(line.strip() for line in m.group(1).splitlines())


class PreviewTargets(unittest.TestCase):
    def test_preview_workflows_only_target_the_preview_host(self):
        for path in (DEPLOY, TEARDOWN, RECONCILE):
            text = read(path)
            for forbidden in ("SERVER_IP", "PROD_SERVER", "neonbee.app", "84.247.131.148", "167.114.98.177"):
                self.assertNotIn(forbidden, text, f"{os.path.basename(path)} mentions {forbidden}")
            self.assertIn("PREVIEW_HOST", text)


def host_config_block(text):
    m = re.search(r"# ── host config from secrets \(keep identical.*?\n(.*?)# ── end host config ──", text, re.S)
    assert m, "host config block not found"
    return "\n".join(line.strip() for line in m.group(1).splitlines())


class HostConfigFromSecrets(unittest.TestCase):
    def test_every_preview_workflow_writes_ci_env_from_secrets_with_the_same_block(self):
        blocks = {}
        for path in (DEPLOY, TEARDOWN, RECONCILE):
            text = read(path)
            self.assertIn("secrets.PREVIEW_DEV_ORIGIN", text, os.path.basename(path))
            self.assertIn("secrets.SUPABASE_URL }}", text, os.path.basename(path))
            blocks[path] = host_config_block(text)
        self.assertEqual(len(set(blocks.values())), 1, "host config blocks differ between workflows")

    def test_the_block_validates_values_and_writes_ci_env_atomically_and_privately(self):
        block = host_config_block(read(DEPLOY))
        self.assertIn("grep -qxE '[A-Za-z0-9.-]+'", block)
        self.assertIn("umask 077", block)
        self.assertIn("mv /etc/so360-preview/ci.env.tmp /etc/so360-preview/ci.env", block)

    def test_the_workflows_declare_the_new_secrets(self):
        self.assertRegex(read(DEPLOY), r"\n\s+PREVIEW_DEV_ORIGIN:\s+\{ required: false \}")
        teardown = read(TEARDOWN)
        self.assertRegex(teardown, r"\n\s+PREVIEW_DEV_ORIGIN:\s+\{ required: false \}")
        self.assertRegex(teardown, r"\n\s+SUPABASE_URL:\s+\{ required: false \}")

    def test_preview_ctl_reads_ci_env_after_preview_env_so_secrets_win(self):
        text = read(CTL)
        self.assertIn("PREVIEW_ENV_FILE=${PREVIEW_ENV_FILE:-/etc/so360-preview/preview.env}", text)
        self.assertIn("PREVIEW_CI_ENV_FILE=${PREVIEW_CI_ENV_FILE:-/etc/so360-preview/ci.env}", text)
        self.assertLess(text.index('. "$PREVIEW_ENV_FILE"'), text.index('. "$PREVIEW_CI_ENV_FILE"'))


class BackendDatabase(unittest.TestCase):
    def test_backend_uses_the_preview_database_first_then_dev_then_prod(self):
        # User decision 2026-09-30: no preview database exists yet, so backend
        # previews fall back to the dev/prod database exactly like dev does.
        text = read(DEPLOY)
        env_block = text.split('> "$OUT/app/.env"')[0].rsplit("if [ \"$KIND\" = be ]", 1)[1]
        for var, secret in (("SUPABASE_URL", "SUPABASE_URL"),
                            ("SUPABASE_ANON_KEY", "SUPABASE_ANON_KEY"),
                            ("SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_KEY")):
            self.assertIn(
                f'echo "{var}=${{{{ secrets.{secret}_PREVIEW || secrets.{secret}_DEV || secrets.{secret} }}}}"',
                env_block)

    def test_backend_on_the_production_database_warns_on_every_run(self):
        text = read(DEPLOY)
        self.assertIn("Preview backend is on the PRODUCTION database", text)
        self.assertRegex(text, r'\[ -z "\$PVURL" \]')

    def test_the_host_guard_is_disarmed_by_an_empty_prod_supabase_host(self):
        for path in (DEPLOY, TEARDOWN, RECONCILE):
            self.assertIn("PROD_SUPABASE_HOST=\\n", read(path), os.path.basename(path))

    def test_no_mail_keys_reach_a_preview_backend(self):
        text = read(DEPLOY)
        for key in ("SES_", "SMTP", "AWS_SECRET", "MAIL_"):
            self.assertNotIn(key, text)

    def test_preview_ctl_refuses_the_production_database(self):
        text = read(CTL)
        self.assertIn('grep -qF "$PROD_SUPABASE_HOST"', text)

    def test_preview_ctl_forces_preview_flags(self):
        text = read(CTL)
        for flag in ("SO360_PREVIEW=true", "EVENT_WORKERS_DISABLED=true",
                     "SIGNAL_EVENT_CONSUMER_DISABLED=true", "SIGNAL_RULES_SWEEP=off"):
            self.assertIn(f'echo "{flag}"', text)


class DeployBehaviour(unittest.TestCase):
    def test_newer_push_cancels_older_run(self):
        self.assertRegex(read(DEPLOY), r"cancel-in-progress:\s*true")

    def test_no_preview_marker_skips(self):
        self.assertIn("[no-preview]", read(DEPLOY))

    def test_only_feat_and_fix_branches(self):
        self.assertRegex(read(DEPLOY), r"feat/\*\|fix/\*\)")

    def test_access_app_is_created_before_dns(self):
        text = read(CTL)
        body = text[text.index("cmd_cf_ensure() {"):text.index("cmd_cf_remove() {")]
        self.assertLess(body.index("/access/apps\""), body.index("/dns_records\""))

    def test_teardown_removes_dns_before_access(self):
        text = read(CTL)
        body = text[text.index("cmd_cf_remove() {"):]
        self.assertLess(body.index("dns_records/$id"), body.index("access/apps/$id"))

    def test_deploy_and_teardown_use_the_host_for_cloudflare(self):
        # Private repos get no org secrets; the token lives on the preview host.
        self.assertIn("preview-ctl cf-ensure", read(DEPLOY))
        self.assertIn("preview-ctl cf-remove", read(TEARDOWN))
        for path in (DEPLOY, TEARDOWN):
            self.assertNotIn("api.cloudflare.com", read(path), os.path.basename(path))

    def test_the_host_config_block_writes_the_cloudflare_token_privately(self):
        block = host_config_block(read(RECONCILE))
        self.assertIn("mv /etc/so360-preview/cf.env.tmp /etc/so360-preview/cf.env", block)
        self.assertIn('if [ -n "${CFT:-}" ]', block)

    def test_slug_is_computed_identically(self):
        self.assertEqual(slug_block(read(DEPLOY)), slug_block(read(TEARDOWN)))


class Reconcile(unittest.TestCase):
    def test_never_deletes_git_branches(self):
        text = read(RECONCILE)
        self.assertNotRegex(text, r"-X DELETE[^\n]*git/refs")
        self.assertNotIn("git push --delete", text)

    def test_api_errors_never_count_as_branch_deleted(self):
        self.assertIn('[ "$code" = 404 ] || alive=1', read(RECONCILE))


class MfeStylesheetPath(unittest.TestCase):
    """The federation runtime requests <base>/<file>.css, not <base>/assets/<file>.css."""

    def test_an_mfe_preview_also_serves_its_stylesheet_from_the_root(self):
        text = read(DEPLOY)
        # Only MFE builds (kind fe + is_mfe) get the root copy.
        self.assertIn('if [ "$KIND" = fe ] && [ "${{ inputs.is_mfe }}" = "true" ]; then', text)
        self.assertIn("find dist/assets -maxdepth 1 -type f -name '*.css' -exec cp {} \"$OUT/app/\" \\;", text)

    def test_the_root_copy_is_added_after_dist_is_staged_and_never_replaces_it(self):
        text = read(DEPLOY)
        stage = text.index('cp -r dist/. "$OUT/app/"')
        css = text.index("find dist/assets -maxdepth 1")
        self.assertLess(stage, css)
        # assets/ stays in place — the copy is additive.
        self.assertNotIn("mv dist/assets", text)


if __name__ == "__main__":
    unittest.main()
