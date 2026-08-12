"""Golden-vector and property tests for the Skport signature algorithm.

The algorithm is: HMAC-SHA256 over the concatenation of
path + (query if GET else body) + timestamp + compact-JSON of the
{platform, timestamp, dId, vName} headers, then MD5 of that hex digest.

Golden vectors below were captured from a working session and match the
crypto pipeline (SHA256/HMAC/MD5) used by the reference client bundle in
`references/`. They lock the algorithm so that a refactor cannot silently
change the signing pipeline (header ordering, JSON separators, hashing
order, or dId defaulting).

Run with:  python -m unittest discover -s tests -v
"""
import hashlib
import hmac
import json
import unittest

import main

PATH = '/web/v1/game/endfield/attendance'
TOKEN = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6'
HEADERS = {'platform': '3', 'timestamp': '1755000000',
           'dId': '', 'vName': '1.0.0'}

# (method, query, body, expected_sign) captured from the working script.
GOLDEN = [
    ('GET', '', '', '5b4cee8ac280ad7e7f2f624959730691'),
    ('GET', '?page=1', '', '99647f14d39510e020e4aac7cd45fb30'),
    ('POST', '?ignored=1', '{"a":1}', '965a7b5efa53ec9ea3e2ff055d6d67dc'),
]


def reference_sign(path, method, headers, query, body, token):
    """Independent reimplementation (different code path) of the algorithm.

    Builds the header JSON by hand instead of by dict iteration, so a bug in
    generate_sign's key ordering or dId defaulting shows up as a mismatch.
    """
    parts = []
    for key in ("platform", "timestamp", "dId", "vName"):
        if key in headers:
            parts.append(
                f'"{key}":{json.dumps(headers[key], separators=(",", ":"))}')
        elif key == "dId":
            parts.append('"dId":""')

    string_to_sign = path
    string_to_sign += query if method == "GET" else body
    if "timestamp" in headers:
        string_to_sign += headers["timestamp"]
    string_to_sign += "{" + ",".join(parts) + "}"

    mac = hmac.new(token.encode(), string_to_sign.encode(),
                   hashlib.sha256).digest()
    return hashlib.md5(mac.hex().encode()).hexdigest()


class GenerateSignTest(unittest.TestCase):

    def test_golden_vectors(self):
        """Captured from the working script; any change breaks the server."""
        for method, query, body, expected in GOLDEN:
            with self.subTest(method=method, query=query, body=body):
                sign = main.generate_sign(
                    PATH, method, HEADERS, query, body, TOKEN)
                self.assertEqual(sign, expected)

    def test_matches_reference_implementation(self):
        for method, query, body, _ in GOLDEN:
            with self.subTest(method=method, query=query, body=body):
                self.assertEqual(
                    main.generate_sign(PATH, method, HEADERS,
                                       query, body, TOKEN),
                    reference_sign(PATH, method, HEADERS, query, body, TOKEN),
                )

    def test_deterministic(self):
        a = main.generate_sign(PATH, 'GET', HEADERS, '', '', TOKEN)
        b = main.generate_sign(PATH, 'GET', HEADERS, '', '', TOKEN)
        self.assertEqual(a, b)

    def test_get_uses_query_not_body(self):
        with_query = main.generate_sign(
            PATH, 'GET', HEADERS, '?page=1', '{"a":1}', TOKEN)
        query_only = main.generate_sign(
            PATH, 'GET', HEADERS, '?page=1', '', TOKEN)
        self.assertEqual(with_query, query_only)

    def test_post_uses_body_not_query(self):
        with_body = main.generate_sign(
            PATH, 'POST', HEADERS, '?page=1', '{"a":1}', TOKEN)
        body_only = main.generate_sign(
            PATH, 'POST', HEADERS, '', '{"a":1}', TOKEN)
        self.assertEqual(with_body, body_only)

    def test_token_changes_sign(self):
        other = main.generate_sign(PATH, 'GET', HEADERS, '', '', 'other-token')
        self.assertNotEqual(other, main.generate_sign(
            PATH, 'GET', HEADERS, '', '', TOKEN))

    def test_timestamp_changes_sign(self):
        shifted = dict(HEADERS, timestamp='1755000001')
        self.assertNotEqual(
            main.generate_sign(PATH, 'GET', shifted, '', '', TOKEN),
            main.generate_sign(PATH, 'GET', HEADERS, '', '', TOKEN),
        )

    def test_missing_timestamp_changes_sign(self):
        no_ts = {k: v for k, v in HEADERS.items() if k != 'timestamp'}
        self.assertNotEqual(
            main.generate_sign(PATH, 'GET', no_ts, '', '', TOKEN),
            main.generate_sign(PATH, 'GET', HEADERS, '', '', TOKEN),
        )

    def test_missing_did_defaults_to_empty(self):
        """dId omitted behaves like dId='' (the real flow never sets it)."""
        no_did = {k: v for k, v in HEADERS.items() if k != 'dId'}
        self.assertEqual(
            main.generate_sign(PATH, 'GET', no_did, '', '', TOKEN),
            main.generate_sign(PATH, 'GET', HEADERS, '', '', TOKEN),
        )

    def test_missing_platform_is_skipped(self):
        """Unlike dId, an absent platform key is omitted from the signed JSON."""
        no_platform = {k: v for k, v in HEADERS.items() if k != 'platform'}
        platform_empty = dict(HEADERS, platform='')
        self.assertNotEqual(
            main.generate_sign(PATH, 'GET', no_platform, '', '', TOKEN),
            main.generate_sign(PATH, 'GET', platform_empty, '', '', TOKEN),
        )

    def test_empty_platform_is_signed_as_empty_string(self):
        no_platform = {k: v for k, v in HEADERS.items() if k != 'platform'}
        platform_empty = dict(HEADERS, platform='')
        self.assertEqual(
            main.generate_sign(PATH, 'GET', platform_empty, '', '', TOKEN),
            reference_sign(PATH, 'GET', platform_empty, '', '', TOKEN),
        )
        self.assertNotEqual(
            main.generate_sign(PATH, 'GET', no_platform, '', '', TOKEN),
            main.generate_sign(PATH, 'GET', platform_empty, '', '', TOKEN),
        )


if __name__ == '__main__':
    unittest.main()
