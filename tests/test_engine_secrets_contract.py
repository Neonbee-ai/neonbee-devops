"""
BDD specs - content-engine and web-research secrets reach the backend .env.

Each name must be declared under on.workflow_call.secrets (required: false) and
written to the service .env only through the existing whitelist pattern.
Run: python3 -m unittest discover tests
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows")
BACKEND = os.path.join(WF, "neonbee-deploy-backend.yml")
PROD = os.path.join(WF, "neonbee-deploy-prod.yml")
PREVIEW = os.path.join(WF, "neonbee-deploy-preview.yml")

ENGINE_SECRETS = [
    "CONTENTFORGE_INTEGRATION_KEY", "LUMEN_INTEGRATION_KEY", "INNGEST_EVENT_KEY",
    "INNGEST_SIGNING_KEY", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "TAVILY_API_KEY",
    "FIRECRAWL_API_KEY", "EXA_API_KEY", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
    "SENTRY_DSN", "POSTHOG_API_KEY", "LUMEN_SITE_RENDER_KEY", "REVALIDATE_SECRET",
    "WEB_SEARCH_PROVIDER", "WEB_SEARCH_API_KEY",
]


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class EngineSecretsForwarded(unittest.TestCase):
    def test_each_deploy_workflow_declares_every_secret_as_optional(self):
        for path in (BACKEND, PROD, PREVIEW):
            text = read(path)
            for name in ENGINE_SECRETS:
                self.assertRegex(text, rf"(?m)^\s+{name}:\s*\{{ required: false \}}",
                                 f"{os.path.basename(path)} does not declare {name}")

    def test_backend_and_prod_write_each_secret_only_when_non_empty(self):
        for path in (BACKEND, PROD):
            text = read(path)
            for name in ENGINE_SECRETS:
                pat = (rf'\[ -n "\$\{{\{{ secrets\.{name} \}}\}}" \]\s+&&\s+echo '
                       rf'"{name}=\$\{{\{{ secrets\.{name} \}}\}}"\s+>> \.env\.ci')
                self.assertRegex(text, pat, f"{os.path.basename(path)} does not guard {name}")

    def test_backend_writes_each_secret_in_both_env_blocks(self):
        text = read(BACKEND)
        for name in ENGINE_SECRETS:
            self.assertEqual(len(re.findall(rf'echo "{name}=', text)), 2, name)

    def test_preview_backend_env_lists_each_secret(self):
        text = read(PREVIEW)
        for name in ENGINE_SECRETS:
            self.assertIn(f'echo "{name}=${{{{ secrets.{name} }}}}"', text, name)


if __name__ == "__main__":
    unittest.main()
