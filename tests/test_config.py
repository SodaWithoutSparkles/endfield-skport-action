"""Tests for config loading, env/json merging, and Profile parsing.

Run with:  python -m unittest discover -s tests -v
"""
import json
import unittest
from unittest import mock

import main

PROFILES_JSON = json.dumps([{
    'SK_OAUTH_CRED_KEY': 'env-cred',
    'SK_TOKEN_CACHE_KEY': 'env-token',
    'gameId': '123',
    'server': '2',
    'language': 'en',
    'accountName': 'Player1',
}])


class ProfileFromDictTest(unittest.TestCase):

    def test_parses_camel_case_keys(self):
        profile = main.Profile.from_dict({
            'SK_OAUTH_CRED_KEY': 'cred',
            'SK_TOKEN_CACHE_KEY': 'token',
            'gameId': '123',
            'server': '9',
            'language': 'ja',
            'accountName': 'P',
            'myDiscordID': '42',
        }, 0)
        self.assertEqual(profile.oauth_cred, 'cred')
        self.assertEqual(profile.token, 'token')
        self.assertEqual(profile.game_id, '123')
        self.assertEqual(profile.server, '9')
        self.assertEqual(profile.language, 'ja')
        self.assertEqual(profile.account_name, 'P')
        self.assertEqual(profile.discord_id, '42')

    def test_defaults(self):
        profile = main.Profile.from_dict({}, 3)
        self.assertEqual(profile.game_id, '')
        self.assertEqual(profile.server, '2')
        self.assertEqual(profile.language, 'en')
        self.assertEqual(profile.account_name, 'Account 4')
        self.assertEqual(profile.discord_id, '')

    def test_game_id_accepts_numbers(self):
        """JSON authors may write gameId as a number; it must stay usable."""
        profile = main.Profile.from_dict({'gameId': 1123123123}, 0)
        self.assertEqual(profile.game_id, '1123123123')


class LoadConfigTest(unittest.TestCase):

    def test_env_profiles_json(self):
        with mock.patch.dict('os.environ', {'SKPORT_PROFILES_JSON': PROFILES_JSON}):
            config = main._load_config_from_env()
        self.assertEqual(len(config['profiles']), 1)
        self.assertTrue(config['discordNotify'])
        self.assertEqual(config['lastSigninDate'], '')

    def test_env_invalid_profiles_json_raises(self):
        with mock.patch.dict('os.environ', {'SKPORT_PROFILES_JSON': '{oops'}):
            with self.assertRaises(ValueError):
                main._load_config_from_env()

    def test_env_profiles_not_list_raises(self):
        with mock.patch.dict('os.environ', {'SKPORT_PROFILES_JSON': '{"a": 1}'}):
            with self.assertRaises(ValueError):
                main._load_config_from_env()

    def test_missing_json_file_yields_empty(self):
        with mock.patch.object(main, 'CONFIG_PATH', 'Z:/does/not/exist.json'):
            self.assertEqual(main._load_config_from_json(), {})

    def test_env_overrides_json(self):
        json_config = {
            'profiles': [{'SK_OAUTH_CRED_KEY': 'json-cred',
                          'SK_TOKEN_CACHE_KEY': 'json-token', 'gameId': '1'}],
            'discordNotify': True,
            'discordWebhook': 'https://json.example/hook',
        }
        with mock.patch.object(main, '_load_config_from_json',
                               return_value=json_config), \
                mock.patch.dict('os.environ', {
                    'SKPORT_PROFILES_JSON': PROFILES_JSON,
                    'DISCORD_WEBHOOK': 'https://env.example/hook',
                }):
            config = main.load_config('both')

        self.assertEqual(config['discordWebhook'], 'https://env.example/hook')
        self.assertEqual(config['profiles'][0]
                         ['SK_OAUTH_CRED_KEY'], 'env-cred')
        self.assertTrue(config['discordNotify'])

    def test_no_profiles_raises(self):
        with mock.patch.object(main, '_load_config_from_json', return_value={}), \
                mock.patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(ValueError):
                main.load_config('both')

    def test_json_only_source_skips_env(self):
        with mock.patch.object(main, '_load_config_from_json',
                               return_value={'profiles': [{}]}), \
                mock.patch.dict('os.environ', {'SKPORT_PROFILES_JSON': PROFILES_JSON}):
            config = main.load_config('json')
        self.assertEqual(len(config['profiles']), 1)
        # Env profile (3 keys) was not used — the json profile wins untouched.
        self.assertEqual(config['profiles'][0], {})


class IsUnsetTest(unittest.TestCase):

    def test_unset_values(self):
        self.assertTrue(main._is_unset('REPLACE_ME'))
        self.assertTrue(main._is_unset('REPLACE_ME with suffix'))
        self.assertTrue(main._is_unset(''))
        self.assertTrue(main._is_unset('   '))
        self.assertTrue(main._is_unset(None))

    def test_set_values(self):
        self.assertFalse(main._is_unset('abc'))
        self.assertFalse(main._is_unset('0'))
        self.assertFalse(main._is_unset(0))


if __name__ == '__main__':
    unittest.main()
