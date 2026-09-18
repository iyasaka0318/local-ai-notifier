import os
import sqlite3
import unittest
from unittest import mock

os.environ.setdefault("NTFY_TOPIC", "test-topic")

import watch_keep
from state_store import ensure_schema


class FakeNote:
    def __init__(self, note_id, title="", text="", dedicated=True):
        self.id = note_id
        self.title = title
        self.text = text
        self.trashed = False
        self.is_dedicated_inbox = dedicated

    def trash(self):
        self.trashed = True


class FakeKeep:
    def __init__(self, notes):
        self._notes = notes
        self.synced = 0

    def all(self):
        return list(self._notes)

    def get(self, note_id):
        return next((n for n in self._notes if n.id == note_id), None)

    def sync(self):
        self.synced += 1


def item(intent, **overrides):
    """A fully-populated classification item, as the schema requires."""
    base = {
        "intent": intent,
        "summary": "",
        "notification_text": None,
        "event_title": None,
        "event_start": None,
        "event_end": None,
        "all_day": False,
        "event_location": None,
        "event_description": None,
        "actionable": True,
        "calendar_ready": False,
        "needs_target_resolution": False,
        "needs_confirmation": False,
        "missing_information": [],
        "scheduled_at": None,
        "recurrence": None,
        "persistent_reminder_action": None,
        "persistent_task_text": None,
        "persistent_target_id": None,
        "persistent_group": None,
        "web_monitor_action": None,
        "web_monitor_target_ids": [],
        "reminder_manage_action": None,
        "reminder_target_ids": [],
        "correction_target": None,
        "correction_action": None,
        "actions": [],
    }
    base.update(overrides)
    return base


class MultiItemNoteTests(unittest.TestCase):
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

    def rows(self, table):
        return self.conn.execute(
            f"SELECT note_id, source_note_id, status FROM {table} ORDER BY note_id"
        ).fetchall()

    def test_one_note_produces_three_separate_items(self):
        """The whole point of 案3: a rambling note must not lose two thirds."""
        note = FakeNote("n1", text="明日9時に歯医者、あと牛乳買うのリマインド、第74回の登録開始も監視して")
        self.run_note(note, [
            item("calendar", summary="歯医者", event_title="歯医者",
                 event_start="2026-10-03T09:00:00+09:00", calendar_ready=True),
            item("persistent_reminder", summary="牛乳を買う",
                 persistent_reminder_action="add", persistent_task_text="牛乳を買う"),
            item("todo", summary="第74回の登録開始を確認する"),
        ])

        self.assertEqual([r[0] for r in self.rows("calendar_jobs")], ["n1"])
        self.assertEqual([r[0] for r in self.rows("todos")], ["n1#2"])
        persistent = self.conn.execute(
            "SELECT source_note_id, task_text FROM persistent_reminders"
        ).fetchall()
        self.assertEqual(persistent, [("n1#1", "牛乳を買う")])

    def test_every_item_is_linked_back_to_its_note(self):
        note = FakeNote("n1", text="二件")
        self.run_note(note, [
            item("memo", summary="覚えておく"),
            item("todo", summary="やる"),
        ])
        self.assertEqual(self.rows("memos"), [("n1", "n1", "active")])
        self.assertEqual(self.rows("todos"), [("n1#1", "n1", "pending")])

    def test_report_covers_all_items_with_one_undo_bundle(self):
        note = FakeNote("n1", text="二件")
        self.run_note(note, [
            item("todo", summary="牛乳"),
            item("memo", summary="パスワードの場所"),
        ])
        entries = self.reports[0]
        self.assertEqual([e["kind"] for e in entries], ["todo", "memo"])
        self.assertEqual(len({e["token"] for e in entries}), 2)

    def test_reminder_without_time_is_executed_not_held(self):
        """案1: a missing time must never end as silent waiting_information."""
        note = FakeNote("n1", text="洗濯するってリマインドして")
        self.run_note(note, [
            item("reminder", summary="洗濯をする", notification_text="洗濯をする"),
        ])
        self.assertEqual(self.rows("reminders"), [])
        saved = self.conn.execute(
            "SELECT task_text FROM persistent_reminders WHERE status = 'active'"
        ).fetchall()
        self.assertEqual(saved, [("洗濯をする",)])
        self.assertIn("継続リマインド", self.reports[0][0]["fallback_reason"])

    def test_unknown_is_kept_as_a_memo(self):
        note = FakeNote("n1", text="よく分からない話")
        self.run_note(note, [item("unknown", summary="よく分からない話")])
        self.assertEqual(self.rows("memos"), [("n1", "n1", "active")])

    def test_editing_a_note_retires_items_it_no_longer_produces(self):
        note = FakeNote("n1", text="三件")
        self.run_note(note, [
            item("todo", summary="A"),
            item("todo", summary="B"),
            item("todo", summary="C"),
        ])
        self.assertEqual(len(self.rows("todos")), 3)

        note.text = "一件だけ"
        self.run_note(note, [item("todo", summary="A")])
        statuses = dict((r[0], r[2]) for r in self.rows("todos"))
        self.assertEqual(statuses["n1"], "pending")
        self.assertEqual(statuses["n1#1"], "superseded")
        self.assertEqual(statuses["n1#2"], "superseded")

    def test_note_waits_for_downstream_work(self):
        note = FakeNote("n1", text="明日の予定を入れて")
        self.run_note(note, [
            item("calendar", summary="歯医者", event_title="歯医者",
                 event_start="2026-10-03T09:00:00+09:00", calendar_ready=True),
        ])
        status = self.conn.execute(
            "SELECT status FROM processed_notes WHERE note_id = 'n1'"
        ).fetchone()[0]
        self.assertEqual(status, "waiting_downstream")
        self.assertFalse(note.trashed)

    def test_local_only_note_is_completed_immediately(self):
        note = FakeNote("n1", text="メモしといて")
        self.run_note(note, [item("memo", summary="覚えておく")])
        status = self.conn.execute(
            "SELECT status FROM processed_notes WHERE note_id = 'n1'"
        ).fetchone()[0]
        self.assertEqual(status, "processed")

    def test_a_failing_item_leaves_no_partial_interpretation(self):
        note = FakeNote("n1", text="二件")
        with mock.patch.object(
            watch_keep, "resolve_web_monitor", side_effect=RuntimeError("検索失敗")
        ):
            self.run_note(note, [
                item("todo", summary="先に書かれるTODO"),
                item("web_monitor", summary="監視したいページ"),
            ])
        self.assertEqual(self.rows("todos"), [])
        self.assertEqual(self.rows("web_monitors"), [])
        status = self.conn.execute(
            "SELECT status FROM processed_notes WHERE note_id = 'n1'"
        ).fetchone()[0]
        self.assertEqual(status, "failed")


if __name__ == "__main__":
    unittest.main()


class CorrectionTests(MultiItemNoteTests):
    """案4: voice users fix a mishearing right after they say it."""

    def register_reminder(self):
        note = FakeNote("n1", text="明日9時に歯医者って通知して")
        self.run_note(note, [
            item("reminder", summary="歯医者", notification_text="歯医者",
                 scheduled_at="2026-10-03T09:00:00+09:00"),
        ])
        return self.reports[0][0]["token"]

    def test_reschedule_moves_the_reminder(self):
        token = self.register_reminder()
        note = FakeNote("n2", text="さっきのリマインダー9時じゃなくて10時")
        self.run_note(note, [
            item("correction", summary="歯医者", correction_target=token,
                 correction_action="reschedule",
                 scheduled_at="2026-10-03T10:00:00+09:00"),
        ])
        scheduled_at, status = self.conn.execute(
            "SELECT scheduled_at, status FROM reminders WHERE note_id = 'n1'"
        ).fetchone()
        self.assertEqual(scheduled_at, "2026-10-03T10:00:00+09:00")
        self.assertEqual(status, "pending")

    def test_cancel_undoes_the_reminder(self):
        token = self.register_reminder()
        note = FakeNote("n2", text="さっきのやっぱりなし")
        self.run_note(note, [
            item("correction", summary="取り消し", correction_target=token,
                 correction_action="cancel"),
        ])
        status = self.conn.execute(
            "SELECT status FROM reminders WHERE note_id = 'n1'"
        ).fetchone()[0]
        self.assertEqual(status, "cancelled")

    def test_rewrite_changes_the_wording(self):
        token = self.register_reminder()
        note = FakeNote("n2", text="さっきのやつ歯医者じゃなくて病院")
        self.run_note(note, [
            item("correction", summary="病院", notification_text="病院",
                 correction_target=token, correction_action="rewrite"),
        ])
        summary = self.conn.execute(
            "SELECT summary FROM reminders WHERE note_id = 'n1'"
        ).fetchone()[0]
        self.assertEqual(summary, "病院")

    def test_a_reschedule_without_a_new_time_fails_the_note(self):
        token = self.register_reminder()
        note = FakeNote("n2", text="さっきの時間変えて")
        self.run_note(note, [
            item("correction", summary="", correction_target=token,
                 correction_action="reschedule"),
        ])
        status = self.conn.execute(
            "SELECT status FROM processed_notes WHERE note_id = 'n2'"
        ).fetchone()[0]
        self.assertEqual(status, "failed")

    def test_recent_actions_are_offered_to_the_classifier(self):
        self.register_reminder()
        captured = {}

        def fake_classify(text, *args):
            captured["recent"] = args[4] if len(args) > 4 else None
            return {"items": [item("memo", summary="x")]}

        note = FakeNote("n2", text="次のメモ")
        with mock.patch.object(watch_keep, "classify_note", side_effect=fake_classify):
            watch_keep.process_note(
                self.conn, self.cur, FakeKeep([note]), note, "google_tasks"
            )
        self.assertEqual(captured["recent"][0]["summary"], "歯医者")
