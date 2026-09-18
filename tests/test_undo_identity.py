import os
import sqlite3
import sys
import unittest
from unittest import mock

os.environ.setdefault("NTFY_TOPIC", "test-topic")
sys.path.insert(0, os.path.dirname(__file__))

import watch_keep
from action_log import record_action, undo_action
from persistent_reminders import add_task
from state_store import ensure_schema
from test_watch_keep_items import FakeKeep, FakeNote, item


class UndoIdentityTests(unittest.TestCase):
    """Undo must reach the item the user pointed at, and nothing else."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.cur = self.conn.cursor()
        ensure_schema(self.conn)
        self.reports = []
        patcher = mock.patch.object(
            watch_keep, "send_execution_report",
            side_effect=lambda topic, entries: self.reports.append(entries) or True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.conn.close()

    def run_note(self, note, items):
        with mock.patch.object(watch_keep, "classify_note", return_value={"items": items}):
            return watch_keep.process_note(
                self.conn, self.cur, FakeKeep([note]), note, "google_tasks"
            )

    def token_for(self, summary, report_index=-1):
        return [e for e in self.reports[report_index] if e["summary"] == summary][0]["token"]

    def test_a_token_from_before_an_edit_is_refused(self):
        """Reordered items reuse the same positional ids, so the old token
        would otherwise cancel whatever now sits at that position."""
        note = FakeNote("n1", text="AとB")
        self.run_note(note, [item("todo", summary="A"), item("todo", summary="B")])
        stale = self.token_for("B")

        note.text = "BとA"
        self.run_note(note, [item("todo", summary="B"), item("todo", summary="A")])

        ok, message = undo_action(self.conn, stale)
        self.conn.commit()
        self.assertFalse(ok)
        self.assertIn("編集された", message)

        statuses = dict(
            self.conn.execute("SELECT summary, status FROM todos").fetchall()
        )
        self.assertEqual(statuses["A"], "pending")
        self.assertEqual(statuses["B"], "pending")

    def test_a_token_from_the_current_generation_still_works(self):
        note = FakeNote("n1", text="AとB")
        self.run_note(note, [item("todo", summary="A"), item("todo", summary="B")])
        ok, _message = undo_action(self.conn, self.token_for("B"))
        self.conn.commit()
        self.assertTrue(ok)
        statuses = dict(
            self.conn.execute("SELECT summary, status FROM todos").fetchall()
        )
        self.assertEqual(statuses["B"], "cancelled")
        self.assertEqual(statuses["A"], "pending")

    def test_undo_reaches_a_deduplicated_persistent_reminder(self):
        """add_task hands back a row created by an earlier note; the undo
        token has to follow it there instead of at its own item id."""
        first = FakeNote("n1", text="牛乳を買うのリマインド")
        self.run_note(first, [item(
            "persistent_reminder", summary="牛乳を買う",
            persistent_reminder_action="add", persistent_task_text="牛乳を買う",
        )])
        second = FakeNote("n2", text="牛乳を買うのリマインド")
        self.run_note(second, [item(
            "persistent_reminder", summary="牛乳を買う",
            persistent_reminder_action="add", persistent_task_text="牛乳を買う",
        )])

        rows = self.conn.execute(
            "SELECT source_note_id FROM persistent_reminders WHERE status = 'active'"
        ).fetchall()
        self.assertEqual(rows, [("n1",)], "重複排除で1行のはず")

        ok, message = undo_action(self.conn, self.token_for("牛乳を買う"))
        self.conn.commit()
        self.assertTrue(ok)
        self.assertIn("取り消しました", message)
        status = self.conn.execute(
            "SELECT status FROM persistent_reminders WHERE source_note_id = 'n1'"
        ).fetchone()[0]
        self.assertEqual(status, "cancelled")


class CorrectionRecordTests(UndoIdentityTests):
    def register_reminder(self):
        note = FakeNote("n1", text="明日9時に歯医者って通知して")
        self.run_note(note, [item(
            "reminder", summary="歯医者", notification_text="歯医者",
            scheduled_at="2026-10-03T09:00:00+09:00",
        )])
        return self.token_for("歯医者")

    def test_a_correction_is_itself_logged(self):
        """Otherwise a second 'さっきの' resolves against the replaced wording."""
        token = self.register_reminder()
        note = FakeNote("n2", text="さっきのやつ病院ね")
        self.run_note(note, [item(
            "correction", summary="病院", notification_text="病院",
            correction_target=token, correction_action="rewrite",
        )])
        kinds = [
            row[0] for row in self.conn.execute(
                "SELECT kind FROM action_log ORDER BY created_at DESC"
            ).fetchall()
        ]
        self.assertEqual(kinds[0], "correction")

    def test_correcting_a_missing_row_is_not_reported_as_success(self):
        token = self.register_reminder()
        self.conn.execute("DELETE FROM reminders")
        note = FakeNote("n2", text="さっきの10時に")
        self.run_note(note, [item(
            "correction", summary="歯医者", correction_target=token,
            correction_action="reschedule", scheduled_at="2026-10-03T10:00:00+09:00",
        )])
        status = self.conn.execute(
            "SELECT status FROM processed_notes WHERE note_id = 'n2'"
        ).fetchone()[0]
        self.assertEqual(status, "failed")


if __name__ == "__main__":
    unittest.main()
