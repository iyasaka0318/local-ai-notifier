import sqlite3
import unittest

from action_log import (
    find_action_by_item,
    get_action,
    record_action,
    recent_actions,
    undo_action,
)
from persistent_reminders import add_task
from state_store import ensure_schema, utc_now


class ActionLogTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def add_reminder(self, note_id="n1"):
        now = utc_now()
        self.conn.execute("""
            INSERT INTO reminders (
                note_id, summary, scheduled_at, status, created_at, updated_at
            ) VALUES (?, '歯医者', '2026-10-03T14:00:00+09:00', 'pending', ?, ?)
        """, (note_id, now, now))
        return record_action(
            self.conn,
            source_note_id=note_id,
            item_id=note_id,
            kind="reminder",
            summary="歯医者",
            detail="2026年10月03日 14:00",
        )

    def test_undo_cancels_a_pending_reminder(self):
        token = self.add_reminder()
        ok, message = undo_action(self.conn, token)
        self.conn.commit()
        self.assertTrue(ok)
        self.assertIn("取り消しました", message)
        status = self.conn.execute(
            "SELECT status FROM reminders WHERE note_id = 'n1'"
        ).fetchone()[0]
        self.assertEqual(status, "cancelled")

    def test_undo_is_idempotent(self):
        token = self.add_reminder()
        undo_action(self.conn, token)
        self.conn.commit()
        ok, message = undo_action(self.conn, token)
        self.conn.commit()
        self.assertTrue(ok)
        self.assertIn("すでに取り消し済み", message)

    def test_undo_reports_already_fired_reminder(self):
        token = self.add_reminder()
        self.conn.execute("UPDATE reminders SET status = 'notified' WHERE note_id = 'n1'")
        ok, message = undo_action(self.conn, token)
        self.conn.commit()
        self.assertTrue(ok)
        self.assertIn("すでに実行済み", message)

    def test_unknown_token_is_reported(self):
        ok, message = undo_action(self.conn, "nope")
        self.assertFalse(ok)
        self.assertIn("見つかりません", message)

    def test_undo_cancels_persistent_reminder(self):
        add_task(self.conn, "p1", "牛乳を買う")
        token = record_action(
            self.conn,
            source_note_id="p1",
            item_id="p1",
            kind="persistent_reminder",
            summary="牛乳を買う",
        )
        ok, _message = undo_action(self.conn, token)
        self.assertTrue(ok)
        status = self.conn.execute(
            "SELECT status FROM persistent_reminders WHERE source_note_id = 'p1'"
        ).fetchone()[0]
        self.assertEqual(status, "cancelled")

    def test_undo_calendar_deletes_the_remote_event(self):
        now = utc_now()
        self.conn.execute("""
            INSERT INTO calendar_jobs (
                note_id, summary, calendar_ready, missing_information, status,
                calendar_event_id, created_at, updated_at
            ) VALUES ('c1', '歯医者', 1, '[]', 'created', 'evt-1', ?, ?)
        """, (now, now))
        token = record_action(
            self.conn, source_note_id="c1", item_id="c1",
            kind="calendar", summary="歯医者",
        )
        deleted = []
        ok, _message = undo_action(self.conn, token, deleted)
        self.conn.commit()
        self.assertTrue(ok)
        self.assertEqual(deleted, [("c1", "evt-1")])
        status = self.conn.execute(
            "SELECT status FROM calendar_jobs WHERE note_id = 'c1'"
        ).fetchone()[0]
        self.assertEqual(status, "cancelled")

    def test_recent_actions_are_newest_first(self):
        record_action(self.conn, source_note_id="a", item_id="a", kind="memo",
                      summary="古い", now="2026-09-18T10:00:00+09:00")
        record_action(self.conn, source_note_id="b", item_id="b", kind="memo",
                      summary="新しい", now="2026-09-18T12:00:00+09:00")
        self.conn.commit()
        entries = recent_actions(self.conn)
        self.assertEqual(entries[0]["summary"], "新しい")

    def test_find_action_by_item_skips_undone(self):
        token = self.add_reminder()
        self.assertEqual(find_action_by_item(self.conn, "n1"), token)
        undo_action(self.conn, token)
        self.conn.commit()
        self.assertIsNone(find_action_by_item(self.conn, "n1"))
        self.assertTrue(get_action(self.conn, token)["undone"])


if __name__ == "__main__":
    unittest.main()
