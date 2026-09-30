"""
BDD specs — the branch → slug rule, executed.

test_preview_contract.py proves the deploy and teardown workflows carry the
same slug block text; these specs run that block through bash and pin what it
produces, so the preview URL (pr-<slug>.skyoffice360.com) is predictable and
always accepted by preview-ctl's slug validator.
Run: python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

import hashlib
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows")
DEPLOY = os.path.join(WF, "neonbee-deploy-preview.yml")
TEARDOWN = os.path.join(WF, "neonbee-preview-teardown.yml")
CTL = os.path.join(ROOT, "preview", "bin", "preview-ctl")

# preview-ctl: valid_slug()
CTL_SLUG = re.compile(r"^[a-z0-9]([a-z0-9-]{0,48}[a-z0-9])?$")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def slug_block(text):
    m = re.search(r"# ── slug \(keep identical.*?\n(.*?\n\s*fi\n)", text, re.S)
    assert m, "slug block not found"
    return "\n".join(line.strip() for line in m.group(1).splitlines())


def _gnu_tools():
    if not shutil.which("bash") or not shutil.which("sha1sum"):
        return False
    return subprocess.run(["sed", "--version"], capture_output=True).returncode == 0


def expected(branch):
    """The documented rule, independently of the shell code."""
    slug = re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")
    if len(slug) > 50:
        slug = slug[:43].rstrip("-") + "-" + hashlib.sha1(branch.encode()).hexdigest()[:6]
    return slug


@unittest.skipUnless(_gnu_tools(), "needs bash, GNU sed and sha1sum (the runner has them)")
class GivenABranchName(unittest.TestCase):
    SOURCES = (DEPLOY, TEARDOWN)

    def slug(self, branch, source=DEPLOY):
        script = slug_block(read(source)) + '\nprintf "%s" "$slug"\n'
        r = subprocess.run(["bash", "-euo", "pipefail", "-c", script], env={**os.environ, "BRANCH": branch, "LC_ALL": "C"},
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_when_short_then_it_is_lowercased_with_separators_collapsed(self):
        cases = {
            "fix/CRM-212-lead-owner": "fix-crm-212-lead-owner",
            "feat/preview-environments": "feat-preview-environments",
            "feat/Foo__Bar..baz": "feat-foo-bar-baz",
            "feat/UPPER/Case/Path": "feat-upper-case-path",
        }
        for branch, slug in cases.items():
            for source in self.SOURCES:
                with self.subTest(branch=branch, source=os.path.basename(source)):
                    self.assertEqual(self.slug(branch, source), slug)

    def test_when_it_has_leading_or_trailing_separators_then_they_are_trimmed(self):
        self.assertEqual(self.slug("-feat/x-"), "feat-x")
        self.assertEqual(self.slug("feat/x///"), "feat-x")

    def test_when_exactly_fifty_characters_then_no_hash_is_added(self):
        branch = "feat/" + "a" * 45
        self.assertEqual(len(expected(branch)), 50)
        self.assertEqual(self.slug(branch), "feat-" + "a" * 45)

    def test_when_longer_than_fifty_then_it_is_cut_to_43_plus_a_sha1_suffix(self):
        branch = "feat/" + "very-long-branch-name-" * 4
        slug = self.slug(branch)
        h = hashlib.sha1(branch.encode()).hexdigest()[:6]
        self.assertTrue(slug.endswith("-" + h), slug)
        self.assertLessEqual(len(slug), 50)
        self.assertEqual(slug, expected(branch))

    def test_when_the_cut_lands_on_a_separator_then_no_double_dash_is_left(self):
        # 42 chars then a '-' at position 43: the cut must not leave "--<hash>".
        branch = "feat/" + "a" * 37 + "-" + "b" * 20
        slug = self.slug(branch)
        self.assertNotIn("--", slug)
        self.assertEqual(slug, expected(branch))

    def test_when_two_long_branches_share_a_prefix_then_their_slugs_differ(self):
        prefix = "feat/" + "shared-prefix-" * 4
        a, b = self.slug(prefix + "alpha"), self.slug(prefix + "beta")
        self.assertNotEqual(a, b)
        self.assertEqual(a[:-7], b[:-7])

    def test_when_computed_twice_or_by_teardown_then_the_slug_is_identical(self):
        branch = "fix/" + "Teardown-Must-Match-Deploy-" * 3
        self.assertEqual(self.slug(branch, DEPLOY), self.slug(branch, DEPLOY))
        self.assertEqual(self.slug(branch, DEPLOY), self.slug(branch, TEARDOWN))

    def test_when_any_feature_branch_is_slugged_then_preview_ctl_accepts_it(self):
        branches = [
            "feat/a", "fix/B", "feat/CRM-212_lead owner", "feat/" + "x" * 120,
            "fix/" + "-" * 30 + "tail", "feat/" + "ab-" * 30, "feat/2026.09.30-hotfix",
        ]
        for branch in branches:
            with self.subTest(branch=branch):
                slug = self.slug(branch)
                self.assertRegex(slug, CTL_SLUG)
                self.assertEqual(slug, expected(branch))

    def test_when_the_host_is_built_then_it_is_a_single_dns_label(self):
        slug = self.slug("feat/" + "z" * 200)
        self.assertLessEqual(len("pr-" + slug), 63)


if __name__ == "__main__":
    unittest.main()
