"""
BDD specs — the CDN cache-header contract for Module Federation.

Background
----------
Module Federation clients resolve chunk URLs out of `remoteEntry.js`, which is
served `no-cache`. A chunk URL is therefore only ever as good as the client's
cache. Deploys rsync assets onto the VM, so there is a window in which a fresh
remoteEntry.js names a chunk that has not landed yet and nginx answers 404.

That 404 is harmless *unless it is stored*. nginx's `add_header` normally skips
error responses; the `always` flag forces it onto them. With
`add_header Cache-Control "public, max-age=31536000, immutable" always;` a
deploy-window 404 is cached for a year, so the client keeps failing long after
the file exists — while every other client, and curl, sees a healthy 200.

That is the failure these specs lock out. Run: python3 -m unittest discover tests
"""

import os
import re
import unittest

NGINX_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nginx")

# The configs that front Module Federation bundles.
CDN_CONFS = [
    "cdn.neonbee.app.conf",
    "ha-cdn.neonbee.app.conf",
    "dev-cdn.skyoffice360.com.conf",
]

# `add_header Cache-Control "<value>" [always];`
CACHE_HEADER = re.compile(
    r'add_header\s+Cache-Control\s+"(?P<value>[^"]*)"\s*(?P<always>always)?\s*;',
    re.IGNORECASE,
)


def read_conf(name):
    with open(os.path.join(NGINX_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


class GivenACdnServingHashedAssets(unittest.TestCase):
    """The immutable long-cache headers must never reach an error response."""

    def test_when_cache_control_is_immutable_then_it_does_not_use_always(self):
        for conf in CDN_CONFS:
            body = read_conf(conf)
            for match in CACHE_HEADER.finditer(body):
                value = match.group("value").lower()
                if "immutable" not in value:
                    continue
                self.assertIsNone(
                    match.group("always"),
                    f"{conf}: `add_header Cache-Control \"{match.group('value')}\" always` "
                    "attaches a long-lived immutable header to 404s as well. A chunk "
                    "missed during the deploy rsync window would then be cached as "
                    "permanently absent. Drop `always` from this header.",
                )

    def test_when_an_asset_is_missing_then_the_404_is_explicitly_not_stored(self):
        for conf in CDN_CONFS:
            body = read_conf(conf)
            self.assertIn(
                "error_page 404 = @notfound",
                body,
                f"{conf}: no @notfound handler — a 404 falls back to whatever "
                "caching headers are in scope instead of an explicit no-store.",
            )
            handler = body.split("location @notfound", 1)[1]
            handler = handler.split("}", 1)[0]
            self.assertRegex(
                handler,
                r'Cache-Control\s+"no-store"',
                f"{conf}: @notfound must send Cache-Control \"no-store\" so a "
                "transiently-missing chunk is retried on the next page load.",
            )


class GivenAModuleFederationHost(unittest.TestCase):
    """remoteEntry.js is the manifest — caching it strands clients on dead hashes."""

    def test_then_remote_entry_is_never_cached(self):
        for conf in CDN_CONFS:
            body = read_conf(conf)
            self.assertIn(
                "remoteEntry",
                body,
                f"{conf}: no remoteEntry.js location block.",
            )
            block = body.split("remoteEntry", 1)[1].split("}", 1)[0]
            self.assertIn(
                "no-cache",
                block,
                f"{conf}: remoteEntry.js must be no-cache, otherwise clients keep "
                "resolving chunk hashes from a stale manifest after a deploy.",
            )


class GivenACrossOriginModuleLoad(unittest.TestCase):
    """CORS must survive on errors, or the browser masks the real status."""

    def test_then_allow_origin_is_sent_even_on_error_responses(self):
        for conf in CDN_CONFS:
            body = read_conf(conf)
            acao = re.findall(
                r'add_header\s+Access-Control-Allow-Origin\s+"[^"]*"\s*(always)?\s*;',
                body,
                re.IGNORECASE,
            )
            self.assertTrue(acao, f"{conf}: no Access-Control-Allow-Origin header.")
            self.assertTrue(
                all(flag == "always" for flag in acao),
                f"{conf}: every Access-Control-Allow-Origin must keep `always`. "
                "Without it a 404 arrives without CORS and the browser reports an "
                "opaque CORS failure instead of the real 404.",
            )


if __name__ == "__main__":
    unittest.main()
