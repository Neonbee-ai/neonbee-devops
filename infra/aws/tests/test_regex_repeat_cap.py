"""BDD: bash/BSD (macOS Mini runners) regex caps {m,n} repetition at 255."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
REP = re.compile(r"\{(\d*),?(\d*)\}")
CAP = 255


def files():
    for pat in (".github/workflows/*.yml", ".github/workflows/*.yaml", "**/*.sh"):
        yield from ROOT.glob(pat)


class RegexRepeatCapTest(unittest.TestCase):
    def test_given_bash_regex_lines_when_scanned_then_no_repeat_count_exceeds_255(self):
        bad = []
        for f in files():
            if ".git/" in str(f):
                continue
            for n, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if "=~" not in line:
                    continue
                for m in REP.finditer(line):
                    if any(g and int(g) > CAP for g in m.groups()):
                        bad.append(f"{f.relative_to(ROOT)}:{n}: {m.group(0)}")
        self.assertEqual(bad, [])

    def test_given_secret_id_regex_when_max_length_then_compiles_under_cap(self):
        wf = (ROOT / ".github/workflows/neonbee-sync-secrets-aws.yml").read_text()
        self.assertIn("@-]{1,255}$", wf)


if __name__ == "__main__":
    unittest.main()
