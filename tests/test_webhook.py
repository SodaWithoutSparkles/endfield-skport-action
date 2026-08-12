"""Tests for Discord webhook delivery and the 2000-char content cap.

Run with:  python -m unittest discover -s tests -v
"""
import json
import unittest
from unittest import mock

import main

WEBHOOK = 'https://discord.example/hook'


class FakeResponse:
    """Context-manager stand-in for urlopen's response."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def capture_urlopen():
    """Returns (captured_payload, urlopen_fake) for asserting webhook posts."""
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured['payload'] = json.loads(req.data.decode('utf-8'))
        return FakeResponse()

    return captured, fake_urlopen


class PostWebhookTest(unittest.TestCase):

    def test_unset_webhook_is_noop(self):
        with mock.patch.object(main.urllib.request, 'urlopen') as urlopen:
            main.post_webhook('REPLACE_ME', 'hello')
            main.post_webhook('', 'hello')
        urlopen.assert_not_called()

    def test_short_content_unchanged(self):
        captured, fake = capture_urlopen()
        with mock.patch.object(main.urllib.request, 'urlopen', fake):
            main.post_webhook(WEBHOOK, 'hello')
        self.assertEqual(captured['payload']['content'], 'hello')
        self.assertEqual(captured['payload']['username'],
                         'Skport Endfield Auto-Sign')

    def test_long_content_truncated(self):
        captured, fake = capture_urlopen()
        with mock.patch.object(main.urllib.request, 'urlopen', fake):
            main.post_webhook(WEBHOOK, 'x' * 5000)
        content = captured['payload']['content']
        self.assertTrue(content.endswith('...[truncated]'))
        # cap + the truncation note stays under Discord's 2000-char limit
        self.assertLessEqual(len(content), 2000)

    def test_boundary_content_not_truncated(self):
        """Exactly at the cap is sent verbatim; over the cap is truncated."""
        captured, fake = capture_urlopen()
        with mock.patch.object(main.urllib.request, 'urlopen', fake):
            main.post_webhook(WEBHOOK, 'x' * main.DISCORD_CONTENT_CAP)
        self.assertEqual(len(captured['payload']['content']),
                         main.DISCORD_CONTENT_CAP)

    def test_webhook_failure_only_warns(self):
        """A failing webhook must not fail the run (it is best-effort)."""
        def failing_urlopen(req, timeout=0):
            raise OSError('connection refused')

        with mock.patch.object(main.urllib.request, 'urlopen', failing_urlopen):
            # Should not raise
            main.post_webhook(WEBHOOK, 'hello')

    def test_webhook_url_sanitized_in_logs(self):
        """The webhook URL (which carries a secret token) never reaches the logs."""
        def failing_urlopen(req, timeout=0):
            raise OSError(f'connection refused to {WEBHOOK}')

        with mock.patch.object(main.urllib.request, 'urlopen', failing_urlopen), \
                self.assertLogs(main.logger, level='WARNING') as logs:
            main.post_webhook(WEBHOOK, 'hello')
        self.assertNotIn(WEBHOOK, '\n'.join(logs.output))
        self.assertIn('[REDACTED]', '\n'.join(logs.output))


class SanitizeTest(unittest.TestCase):

    def test_redacts_secrets(self):
        text = 'error with secret-token and secret-cred inside'
        safe = main._sanitize(text, ('secret-token', 'secret-cred'))
        self.assertNotIn('secret-token', safe)
        self.assertNotIn('secret-cred', safe)
        self.assertIn('[REDACTED]', safe)

    def test_empty_secrets_noop(self):
        text = 'nothing to hide'
        self.assertEqual(main._sanitize(text, ('', None)), text)


class NotifyUserTest(unittest.TestCase):

    def test_ping_inserted_only_in_discord_content(self):
        """The mention is added by notify_user, not by the flow messages."""
        captured, fake = capture_urlopen()
        results = [("Check-in completed for Player1\nEndfield: OK", "12345")]
        with mock.patch.object(main.urllib.request, 'urlopen', fake):
            main.notify_user(WEBHOOK, results)
        self.assertEqual(
            captured['payload']['content'],
            "Check-in completed for Player1\nEndfield: <@12345> OK")

    def test_no_ping_when_id_unset(self):
        captured, fake = capture_urlopen()
        results = [
            ("Check-in completed for Player1\nEndfield: OK", "REPLACE_ME")]
        with mock.patch.object(main.urllib.request, 'urlopen', fake):
            main.notify_user(WEBHOOK, results)
        self.assertEqual(
            captured['payload']['content'],
            "Check-in completed for Player1\nEndfield: OK")

    def test_message_without_endfield_line_passes_through(self):
        """Skip-style messages have no mention target; leave them untouched."""
        captured, fake = capture_urlopen()
        results = [
            ("[Profile 1] Skip: Missing configuration credentials.", "12345")]
        with mock.patch.object(main.urllib.request, 'urlopen', fake):
            main.notify_user(WEBHOOK, results)
        self.assertEqual(
            captured['payload']['content'],
            "[Profile 1] Skip: Missing configuration credentials.")

    def test_multiple_results_joined_with_individual_pings(self):
        captured, fake = capture_urlopen()
        results = [
            ("Check-in completed for A\nEndfield: OK", "1"),
            ("Check-in completed for B\nEndfield: OK", ""),
        ]
        with mock.patch.object(main.urllib.request, 'urlopen', fake):
            main.notify_user(WEBHOOK, results)
        self.assertEqual(
            captured['payload']['content'],
            "Check-in completed for A\nEndfield: <@1> OK\n\n"
            "Check-in completed for B\nEndfield: OK")

    def test_unset_webhook_is_noop(self):
        with mock.patch.object(main.urllib.request, 'urlopen') as urlopen:
            main.notify_user('', [("x", "1")])
        urlopen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
