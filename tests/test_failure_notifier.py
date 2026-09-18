import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from failure_notifier import notify_processing_failure
from state_store import ensure_schema


class FailureNotifierTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ensure_schema(self.conn)
        self.sent = []

    def tearDown(self):
        self.conn.close()

    def sender(self, topic, message, title):
        self.sent.append((topic, message, title))

    def test_same_failure_is_suppressed_during_cooldown(self):
        now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(notify_processing_failure(
            self.conn, "topic", "調査処理", "1", TimeoutError("timed out"),
            sender=self.sender, now=now,
        ))
        self.assertFalse(notify_processing_failure(
            self.conn, "topic", "調査処理", "1", TimeoutError("timed out"),
            sender=self.sender, now=now + timedelta(minutes=5),
        ))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("通信タイムアウト", self.sent[0][1])
        self.assertIn("自動で再試行します", self.sent[0][1])

    def test_error_details_are_redacted(self):
        notify_processing_failure(
            self.conn,
            "topic",
            "カレンダー登録",
            "x",
            ValueError("token=abc123 https://example.com/?secret=xyz"),
            retrying=False,
            sender=self.sender,
        )
        message = self.sent[0][1]
        self.assertNotIn("abc123", message)
        self.assertNotIn("example.com", message)
        self.assertIn("自動再試行は行いません", message)

    def test_notification_delivery_failure_does_not_raise_or_repeat(self):
        calls = []

        def broken_sender(*args):
            calls.append(args)
            raise RuntimeError("ntfy unavailable")

        now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(notify_processing_failure(
            self.conn, "topic", "処理", "x", RuntimeError("failed"),
            sender=broken_sender, now=now,
        ))
        self.assertFalse(notify_processing_failure(
            self.conn, "topic", "処理", "x", RuntimeError("failed"),
            sender=broken_sender, now=now + timedelta(minutes=1),
        ))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
