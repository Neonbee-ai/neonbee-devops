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
        self.assertIn("grep -qvxE '[A-Za-z0-9.-]+'", block)
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
    def test_backend_requires_a_preview_database_with_no_fallback(self):
        text = read(DEPLOY)
        self.assertIn("SUPABASE_URL_PREVIEW", text)
        self.assertRegex(text, r'KIND" = be \] && \[ -z "\$DBURL" \]')
        # The backend .env must never be written from the dev/prod DB secrets.
        env_block = text.split('> "$OUT/app/.env"')[0].rsplit("if [ \"$KIND\" = be ]", 1)[1]
        self.assertNotRegex(env_block, r"secrets\.SUPABASE_(URL|ANON_KEY|SERVICE_KEY)(_DEV)?\s*[}|]")

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
        text = read(DEPLOY)
        self.assertLess(text.index("/access/apps\""), text.index("/dns_records\""))

    def test_teardown_removes_dns_before_access(self):
        text = read(TEARDOWN)
        self.assertLess(text.index("dns_records/$id"), text.index("access/apps/$id"))

    def test_slug_is_computed_identically(self):
        self.assertEqual(slug_block(read(DEPLOY)), slug_block(read(TEARDOWN)))


class Reconcile(unittest.TestCase):
    def test_never_deletes_git_branches(self):
        text = read(RECONCILE)
        self.assertNotRegex(text, r"-X DELETE[^\n]*git/refs")
        self.assertNotIn("git push --delete", text)

    def test_api_errors_never_count_as_branch_deleted(self):
        self.assertIn('[ "$code" = 404 ] || alive=1', read(RECONCILE))


if __name__ == "__main__":
    unittest.main()
