import sqlite3
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from persistent_reminders import add_task, complete_task, create_notify_now_command
from reminder_worker import (
    dispatch_due_reminders,
    dispatch_persistent_now,
    dispatch_persistent_reminders,
)
from state_store import ensure_schema


JST = ZoneInfo("Asia/Tokyo")


class ReminderWorkerTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def add_reminder(self, note_id, scheduled_at):
        self.conn.execute("""
            INSERT INTO reminders (
                note_id, summary, scheduled_at, status, created_at, updated_at
            ) VALUES (?, 'test reminder', ?, 'pending', 'now', 'now')
        """, (note_id, scheduled_at))
        self.conn.commit()

    def test_only_due_reminder_is_sent(self):
        self.add_reminder("due", "2026-09-17T07:59:00+09:00")
        self.add_reminder("future", "2026-09-17T09:00:00+09:00")
        sent = []

        count = dispatch_due_reminders(
            self.conn,
            "secret-topic",
            now=datetime(2026, 9, 17, 8, 0, tzinfo=JST),
            sender=lambda topic, summary, title: sent.append((topic, summary, title)),
        )

        self.assertEqual(count, 1)
        self.assertEqual(sent, [("secret-topic", "test reminder", "リマインダー")])
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM reminders WHERE note_id = 'due'"
            ).fetchone()[0],
            "notified",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM reminders WHERE note_id = 'future'"
            ).fetchone()[0],
            "pending",
        )

    def test_notification_failure_stays_pending(self):
        self.add_reminder("due", "2026-09-17T07:59:00+09:00")

        def fail(_topic, _summary, _title):
            raise RuntimeError("offline")

        count = dispatch_due_reminders(
            self.conn,
            "secret-topic",
            now=datetime(2026, 9, 17, 8, 0, tzinfo=JST),
            sender=fail,
        )
        row = self.conn.execute("""
            SELECT status, last_error FROM reminders WHERE note_id = 'due'
        """).fetchone()
        self.assertEqual(count, 0)
        self.assertEqual(row, ("pending", "offline"))

    def test_weekly_reminder_is_rescheduled(self):
        self.conn.execute("""
            INSERT INTO reminders (
                note_id, summary, scheduled_at, recurrence, status,
                created_at, updated_at
            ) VALUES (
                'weekly', 'weekly reminder', '2026-09-17T07:59:00+09:00',
                'weekly:WE', 'pending', 'now', 'now'
            )
        """)
        self.conn.commit()
        sent = []
        dispatch_due_reminders(
            self.conn,
            "topic",
            now=datetime(2026, 9, 17, 8, 0, tzinfo=JST),
            sender=lambda topic, summary, title: sent.append(summary),
        )
        row = self.conn.execute("""
            SELECT status, scheduled_at FROM reminders WHERE note_id = 'weekly'
        """).fetchone()
        self.assertEqual(sent, ["weekly reminder"])
        self.assertEqual(row, ("pending", "2026-09-24T07:59:00+09:00"))

    def test_daily_reminder_skips_missed_occurrences_after_outage(self):
        self.conn.execute("""
            INSERT INTO reminders (
                note_id, summary, scheduled_at, recurrence, status,
                created_at, updated_at
            ) VALUES (
                'daily', 'daily reminder', '2026-09-14T08:00:00+09:00',
                'daily', 'pending', 'now', 'now'
            )
        """)
        self.conn.commit()
        sent = []
        dispatch_due_reminders(
            self.conn,
            "topic",
            now=datetime(2026, 9, 17, 9, 0, tzinfo=JST),
            sender=lambda topic, summary, title: sent.append(summary),
        )
        row = self.conn.execute("""
            SELECT status, scheduled_at FROM reminders WHERE note_id = 'daily'
        """).fetchone()
        self.assertEqual(sent, ["daily reminder"])
        self.assertEqual(row, ("pending", "2026-09-18T08:00:00+09:00"))

    def test_persistent_tasks_send_separate_notifications_at_each_slot(self):
        created = "2026-09-17T09:00:00+09:00"
        add_task(self.conn, "a", "バイト先にお菓子を買う", now=created)
        add_task(self.conn, "b", "郵便物を出す", now=created)
        self.conn.commit()
        sent = []
        sender = lambda topic, text, title: sent.append((text, title))

        first = dispatch_persistent_reminders(
            self.conn, "topic", datetime(2026, 9, 17, 11, 0, tzinfo=JST), sender
        )
        duplicate = dispatch_persistent_reminders(
            self.conn, "topic", datetime(2026, 9, 17, 12, 0, tzinfo=JST), sender
        )
        second = dispatch_persistent_reminders(
            self.conn, "topic", datetime(2026, 9, 17, 23, 0, tzinfo=JST), sender
        )

        self.assertEqual((first, duplicate, second), (2, 0, 2))
        self.assertEqual(
            [text for text, _title in sent],
            [
                "バイト先にお菓子を買う", "郵便物を出す",
                "バイト先にお菓子を買う", "郵便物を出す",
            ],
        )

    def test_task_added_after_eleven_waits_until_twenty_three(self):
        add_task(
            self.conn,
            "late",
            "牛乳を買う",
            now="2026-09-17T12:00:00+09:00",
        )
        self.conn.commit()
        sent = []
        sender = lambda topic, text, title: sent.append(text)
        self.assertEqual(
            dispatch_persistent_reminders(
                self.conn, "topic", datetime(2026, 9, 17, 15, 0, tzinfo=JST), sender
            ),
            0,
        )
        self.assertEqual(
            dispatch_persistent_reminders(
                self.conn, "topic", datetime(2026, 9, 17, 23, 0, tzinfo=JST), sender
            ),
            1,
        )
        self.assertEqual(sent, ["牛乳を買う"])

    def test_completed_task_is_no_longer_notified(self):
        task_id = add_task(
            self.conn,
            "task-note",
            "バイト先にお菓子を買う",
            now="2026-09-17T09:00:00+09:00",
        )
        complete_task(
            self.conn,
            "complete-note",
            target_task_id=task_id,
            task_text="バイト先にお菓子を買う",
        )
        self.conn.commit()
        sent = []
        count = dispatch_persistent_reminders(
            self.conn,
            "topic",
            datetime(2026, 9, 17, 11, 0, tzinfo=JST),
            lambda topic, text, title: sent.append(text),
        )
        self.assertEqual(count, 0)
        self.assertEqual(sent, [])

    def test_notify_now_sends_each_active_task_once(self):
        add_task(self.conn, "a", "タスクA", now="2026-09-17T09:00:00+09:00")
        add_task(self.conn, "b", "タスクB", now="2026-09-17T09:00:00+09:00")
        create_notify_now_command(self.conn, "command")
        self.conn.commit()
        sent = []
        sender = lambda topic, text, title: sent.append(text)

        self.assertEqual(
            dispatch_persistent_now(self.conn, "topic", "command", sender),
            2,
        )
        self.assertEqual(
            dispatch_persistent_now(self.conn, "topic", "command", sender),
            0,
        )
        self.assertEqual(sent, ["タスクA", "タスクB"])

    def test_notify_now_prevents_duplicate_in_the_same_scheduled_slot(self):
        add_task(
            self.conn,
            "a",
            "タスクA",
            now="2026-09-17T09:00:00+09:00",
        )
        create_notify_now_command(self.conn, "command")
        self.conn.commit()
        sent = []
        sender = lambda topic, text, title: sent.append(text)
        now = datetime(2026, 9, 17, 11, 5, tzinfo=JST)

        dispatch_persistent_now(
            self.conn, "topic", "command", sender=sender, now=now
        )
        scheduled = dispatch_persistent_reminders(
            self.conn, "topic", now=now, sender=sender
        )

        self.assertEqual(scheduled, 0)
        self.assertEqual(sent, ["タスクA"])


if __name__ == "__main__":
    unittest.main()
