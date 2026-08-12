"""Tests for the token refresh mechanism: refresh is a 401 handler.

SK_TOKEN_CACHE_KEY is optional. When it is missing, the request goes straight
to a refresh; when the server rejects it (HTTP 401 or business code 10000),
the token is refreshed once and the request is retried with the fresh token.
A failed refresh means SK_OAUTH_CRED_KEY has expired or changed (the profile
fails); a refresh that succeeds but still gets rejected means both keys are
out of sync. Refreshed tokens are persisted back into config.json when it
exists.

Run with:  python -m unittest discover -s tests -v
Also runs under pytest.
"""
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import main


def make_profile():
    """A fully-configured Profile, mirroring the new camelCase schema.

    Secret values are chosen so they never appear as substrings of the
    message templates (e.g. 'SK_OAUTH_CRED_KEY'), keeping redaction
    assertions unambiguous.
    """
    return main.Profile.from_dict({
        'SK_OAUTH_CRED_KEY': 'oauth-value-7f2a',
        'SK_TOKEN_CACHE_KEY': 'token-value-9c4b',
        'gameId': '123',
        'server': '2',
        'language': 'en',
        'accountName': 'Player1',
    }, 0)


class FakeResponse:
    """Context-manager stand-in for the object returned by urlopen."""

    def __init__(self, payload: dict, status: int = 200):
        self.status = status
        self.body = json.dumps(payload).encode('utf-8')

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code: int) -> urllib.error.HTTPError:
    """Builds the HTTPError urlopen raises for a non-2xx status."""
    return urllib.error.HTTPError(url='', code=code, msg='', hdrs=None, fp=None)


def scripted_urlopen(responses):
    """Returns a urlopen replacement yielding the given responses in order.

    Items may be FakeResponse (returned) or HTTPError instances (raised).
    """
    queue = list(responses)

    def fake(req, **kwargs):
        if not queue:
            raise AssertionError('urlopen called more times than scripted')
        item = queue.pop(0)
        if isinstance(item, urllib.error.HTTPError):
            raise item
        return item

    return fake


class RefreshMechanismTest(unittest.TestCase):
    """The 401 handler: refresh once, retry with the fresh token."""

    def setUp(self):
        self.profile = make_profile()

    def test_expired_401_refresh_still_401_fails(self):
        """GET 401 -> refresh once -> still 401 -> out-of-sync failure."""
        urlopen = scripted_urlopen([http_error(401), http_error(401)])
        with mock.patch.object(main.urllib.request, 'urlopen', urlopen), \
                mock.patch.object(main, 'refresh_cache_token',
                                  return_value='fresh-token') as refresh, \
                mock.patch.object(main, 'persist_refreshed_token') as persist:
            msg, ok, _ = main.checkin_flow(self.profile, 0, '')

        self.assertFalse(ok)
        self.assertIn('out of sync', msg)
        self.assertIn('SK_OAUTH_CRED_KEY', msg)
        # Exactly one refresh attempt, then the failure propagates.
        refresh.assert_called_once_with(self.profile)
        persist.assert_called_once_with(self.profile, 'fresh-token')

    def test_expired_code10000_refresh_still_10000_fails(self):
        """code 10000 -> refresh once -> still 10000 -> out-of-sync failure."""
        urlopen = scripted_urlopen([
            FakeResponse({'code': 10000}),
            FakeResponse({'code': 10000}),
        ])
        with mock.patch.object(main.urllib.request, 'urlopen', urlopen), \
                mock.patch.object(main, 'refresh_cache_token',
                                  return_value='fresh-token') as refresh:
            msg, ok, _ = main.checkin_flow(self.profile, 0, '')

        self.assertFalse(ok)
        self.assertIn('out of sync', msg)
        refresh.assert_called_once_with(self.profile)

    def test_expired_refresh_success_then_checkin(self):
        """GET 401 -> refresh -> retry OK -> the POST reuses the fresh token."""
        signed = []
        urlopen = scripted_urlopen([
            http_error(401),
            FakeResponse({'code': 0, 'message': 'OK',
                          'data': {'calendar': [], 'hasToday': False}}),
            FakeResponse({'code': 0, 'message': 'OK'}),
        ])

        def fake_sign(path, method, headers, query, body, token):
            signed.append(token)
            return 'x'

        with mock.patch.object(main.urllib.request, 'urlopen', urlopen), \
                mock.patch.object(main, 'generate_sign', fake_sign), \
                mock.patch.object(main, 'refresh_cache_token',
                                  return_value='fresh-token') as refresh, \
                mock.patch.object(main, 'persist_refreshed_token') as persist:
            msg, ok, _ = main.checkin_flow(self.profile, 0, '')

        self.assertTrue(ok)
        self.assertIn('Check-in completed', msg)
        # The refresh signs with the old token; GET and POST use the new one.
        self.assertEqual(
            signed, ['token-value-9c4b', 'fresh-token', 'fresh-token'])
        refresh.assert_called_once_with(self.profile)
        persist.assert_called_once_with(self.profile, 'fresh-token')

    def test_expired_post_refresh_then_success(self):
        """GET OK -> POST 401 -> refresh -> retry POST OK."""
        urlopen = scripted_urlopen([
            FakeResponse({'code': 0, 'message': 'OK',
                          'data': {'calendar': [], 'hasToday': False}}),
            http_error(401),
            FakeResponse({'code': 0, 'message': 'OK'}),
        ])
        with mock.patch.object(main.urllib.request, 'urlopen', urlopen), \
                mock.patch.object(main, 'refresh_cache_token',
                                  return_value='fresh-token') as refresh:
            msg, ok, _ = main.checkin_flow(self.profile, 0, '')

        self.assertTrue(ok)
        self.assertIn('Check-in completed', msg)
        refresh.assert_called_once_with(self.profile)

    def test_refresh_failure_fails_profile(self):
        """A failed refresh means the oauth credential expired -> profile fails."""
        urlopen = scripted_urlopen([http_error(401)])
        with mock.patch.object(main.urllib.request, 'urlopen', urlopen), \
                mock.patch.object(main, 'refresh_cache_token',
                                  side_effect=RuntimeError('Refresh HTTP 400: nope')):
            msg, ok, _ = main.checkin_flow(self.profile, 0, '')

        self.assertFalse(ok)
        self.assertIn('SK_OAUTH_CRED_KEY', msg)
        self.assertIn('refresh failed', msg)
        self.assertIn('Re-copy both keys', msg)

    def test_refresh_failure_redacts_secrets(self):
        """Secret values never reach the message even when nested in errors."""
        urlopen = scripted_urlopen([http_error(401)])
        with mock.patch.object(main.urllib.request, 'urlopen', urlopen), \
                mock.patch.object(main, 'refresh_cache_token',
                                  side_effect=RuntimeError(
                                      'Refresh HTTP 400: cred=oauth-value-7f2a '
                                      'token=token-value-9c4b')):
            msg, ok, _ = main.checkin_flow(self.profile, 0, '')

        self.assertFalse(ok)
        self.assertIn('[REDACTED]', msg)
        self.assertNotIn('oauth-value-7f2a', msg)
        self.assertNotIn('token-value-9c4b', msg)

    def test_post_403_is_already_signed_in(self):
        """Status OK -> server rejects the duplicate POST with 403 -> success."""
        urlopen = scripted_urlopen([
            FakeResponse({'code': 0, 'message': 'OK',
                          'data': {'calendar': [], 'hasToday': False}}),
            http_error(403),
        ])
        with mock.patch.object(main.urllib.request, 'urlopen', urlopen):
            msg, ok, _ = main.checkin_flow(self.profile, 0, '')

        self.assertTrue(ok)
        self.assertIn('already signed in', msg)

    def test_get_403_still_fails_closed(self):
        """403 on the status GET is not 'already signed in' -> fail closed."""
        urlopen = scripted_urlopen([http_error(403)])
        with mock.patch.object(main.urllib.request, 'urlopen', urlopen):
            msg, ok, _ = main.checkin_flow(self.profile, 0, '')

        self.assertFalse(ok)
        # Distinct prefix: the status check and the check-in report differently.
        self.assertIn('Status check failed', msg)
        self.assertNotIn('Check-in failed', msg)

    def test_missing_credentials_skip_without_failure(self):
        """Unset secrets -> skip (success, not failure); no network call at all."""
        urlopen = scripted_urlopen([])
        profile = main.Profile.from_dict({'gameId': '1'}, 1)
        with mock.patch.object(main.urllib.request, 'urlopen', urlopen), \
                mock.patch.object(main, 'refresh_cache_token') as refresh:
            msg, ok, _ = main.checkin_flow(profile, 1, '')

        self.assertTrue(ok)
        self.assertIn('Skip: Missing configuration credentials.', msg)
        refresh.assert_not_called()

    def test_missing_token_refreshes_before_first_request(self):
        """No cached token -> refresh via oauth cred, then GET and POST."""
        profile = main.Profile.from_dict({
            'SK_OAUTH_CRED_KEY': 'oauth-value-7f2a',
            'gameId': '123',
            'accountName': 'Player1',
        }, 0)
        urlopen = scripted_urlopen([
            FakeResponse({'code': 0, 'message': 'OK',
                          'data': {'calendar': [], 'hasToday': False}}),
            FakeResponse({'code': 0, 'message': 'OK'}),
        ])
        with mock.patch.object(main.urllib.request, 'urlopen', urlopen), \
                mock.patch.object(main, 'refresh_cache_token',
                                  return_value='fresh-token') as refresh, \
                mock.patch.object(main, 'persist_refreshed_token') as persist:
            msg, ok, _ = main.checkin_flow(profile, 0, '')

        self.assertTrue(ok)
        self.assertIn('Check-in completed', msg)
        refresh.assert_called_once_with(profile)
        persist.assert_called_once_with(profile, 'fresh-token')

    def test_missing_token_refresh_failure_blames_oauth_cred(self):
        """Refresh failing with no cached token never says 'Token expired'."""
        profile = main.Profile.from_dict({
            'SK_OAUTH_CRED_KEY': 'oauth-value-7f2a',
            'gameId': '123',
        }, 0)
        urlopen = scripted_urlopen([])
        with mock.patch.object(main.urllib.request, 'urlopen', urlopen), \
                mock.patch.object(main, 'refresh_cache_token',
                                  side_effect=RuntimeError('Refresh HTTP 400: nope')):
            msg, ok, _ = main.checkin_flow(profile, 0, '')

        self.assertFalse(ok)
        self.assertIn('SK_OAUTH_CRED_KEY', msg)
        self.assertNotIn('Token expired', msg)
        self.assertIn('Re-copy SK_OAUTH_CRED_KEY', msg)

    def test_already_signed_in_skips_post(self):
        """hasToday -> no POST is attempted at all."""
        urlopen = scripted_urlopen([
            FakeResponse({'code': 0, 'message': 'OK',
                          'data': {'calendar': [{'done': True}, {'done': True}],
                                   'hasToday': True}}),
        ])
        with mock.patch.object(main.urllib.request, 'urlopen', urlopen):
            msg, ok, discord_id = main.checkin_flow(self.profile, 0, '42')

        self.assertTrue(ok)
        self.assertIn('2 days claimed', msg)
        self.assertIn('already signed in', msg)
        # The mention is resolved for Discord, but never lands in the log message.
        self.assertEqual(discord_id, '42')
        self.assertNotIn('<@', msg)


class PersistRefreshedTokenTest(unittest.TestCase):
    """persist_refreshed_token updates config.json when it exists."""

    def _write_config(self, path, profiles):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'profiles': profiles, 'discordNotify': True},
                      f, indent=4)

    def test_writes_token_to_matching_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.json')
            self._write_config(path, [
                {'SK_OAUTH_CRED_KEY': 'oauth-value-7f2a',
                 'SK_TOKEN_CACHE_KEY': 'old-token', 'gameId': '123'},
                {'SK_OAUTH_CRED_KEY': 'other-cred',
                 'SK_TOKEN_CACHE_KEY': 'other-token', 'gameId': '456'},
            ])
            with mock.patch.object(main, 'CONFIG_PATH', path):
                ok = main.persist_refreshed_token(make_profile(), 'new-token')

            self.assertTrue(ok)
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            self.assertEqual(
                data['profiles'][0]['SK_TOKEN_CACHE_KEY'], 'new-token')
            # Other profiles and unrelated keys are untouched.
            self.assertEqual(
                data['profiles'][1]['SK_TOKEN_CACHE_KEY'], 'other-token')
            self.assertTrue(data['discordNotify'])

    def test_missing_config_is_noop(self):
        with mock.patch.object(main, 'CONFIG_PATH', 'Z:/does/not/exist.json'):
            self.assertFalse(
                main.persist_refreshed_token(make_profile(), 'new-token'))

    def test_unmatched_cred_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.json')
            self._write_config(path, [
                {'SK_OAUTH_CRED_KEY': 'other-cred',
                 'SK_TOKEN_CACHE_KEY': 'other-token', 'gameId': '456'},
            ])
            with mock.patch.object(main, 'CONFIG_PATH', path):
                ok = main.persist_refreshed_token(make_profile(), 'new-token')

            self.assertFalse(ok)
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            self.assertEqual(
                data['profiles'][0]['SK_TOKEN_CACHE_KEY'], 'other-token')


if __name__ == '__main__':
    unittest.main()
