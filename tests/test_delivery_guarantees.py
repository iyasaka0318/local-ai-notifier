import os
import sqlite3
import sys
import threading
import unittest
from unittest import mock

os.environ.setdefault("NTFY_TOPIC", "test-topic")
sys.path.insert(0, os.path.dirname(__file__))

import tasks_event_listener
import watch_keep
from state_store import drain_reports, ensure_schema, open_reports
from test_watch_keep_items import FakeKeep, FakeNote, item


class ReportDeliveryTests(unittest.TestCase):
    """The report carries the undo button, so it cannot be dropped."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.cur = self.conn.cursor()
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def run_note(self, note, items, sender):
        with mock.patch.object(watch_keep, "send_execution_report", side_effect=sender), \
             mock.patch.object(watch_keep, "classify_note", return_value={"items": items}):
            return watch_keep.process_note(
                self.conn, self.cur, FakeKeep([note]), note, "google_tasks"
            )

    def test_a_failed_report_stays_queued(self):
        def offline(topic, entries):
            raise RuntimeError("ntfy unavailable")

        note = FakeNote("n1", text="牛乳を買う")
        self.run_note(note, [item("todo", summary="牛乳を買う")], offline)

        queued = open_reports(self.conn)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["entries"][0]["summary"], "牛乳を買う")

    def test_the_queued_report_is_resent_later(self):
        def offline(topic, entries):
            raise RuntimeError("ntfy unavailable")

        note = FakeNote("n1", text="牛乳を買う")
        self.run_note(note, [item("todo", summary="牛乳を買う")], offline)

        delivered = []
        sent, failed = drain_reports(
            self.conn, lambda entries: delivered.append(entries) or True
        )
        self.assertEqual((sent, failed), (1, 0))
        self.assertEqual(delivered[0][0]["summary"], "牛乳を買う")
        self.assertEqual(open_reports(self.conn), [])

    def test_the_resent_report_still_carries_its_undo_token(self):
        def offline(topic, entries):
            raise RuntimeError("offline")

        note = FakeNote("n1", text="牛乳を買う")
        self.run_note(note, [item("todo", summary="牛乳を買う")], offline)
        queued = open_reports(self.conn)[0]
        self.assertTrue(queued["entries"][0]["token"])

    def test_a_delivered_report_is_not_resent(self):
        note = FakeNote("n1", text="牛乳を買う")
        self.run_note(note, [item("todo", summary="牛乳を買う")], lambda t, e: True)
        self.assertEqual(open_reports(self.conn), [])


class OrphanCleanupTests(ReportDeliveryTests):
    """Re-classifying a note must retire every kind it no longer produces."""

    def test_a_persistent_reminder_is_retired_when_the_intent_changes(self):
        note = FakeNote("n1", text="牛乳買うのリマインド")
        self.run_note(note, [item(
            "persistent_reminder", summary="牛乳を買う",
            persistent_reminder_action="add", persistent_task_text="牛乳を買う",
        )], lambda t, e: True)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM persistent_reminders WHERE source_note_id='n1'"
            ).fetchone()[0],
            "active",
        )

        note.text = "ただのメモ"
        self.run_note(note, [item("memo", summary="ただのメモ")], lambda t, e: True)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM persistent_reminders WHERE source_note_id='n1'"
            ).fetchone()[0],
            "superseded",
        )

    def test_a_dropped_item_retires_its_persistent_reminder(self):
        note = FakeNote("n1", text="二件")
        self.run_note(note, [
            item("memo", summary="覚える"),
            item("persistent_reminder", summary="牛乳を買う",
                 persistent_reminder_action="add", persistent_task_text="牛乳を買う"),
        ], lambda t, e: True)
        note.text = "一件"
        self.run_note(note, [item("memo", summary="覚える")], lambda t, e: True)
        statuses = [
            r[0] for r in self.conn.execute(
                "SELECT status FROM persistent_reminders"
            ).fetchall()
        ]
        self.assertEqual(statuses, ["superseded"])


class TransactionScopeTests(ReportDeliveryTests):
    def test_the_web_lookup_runs_outside_the_write_transaction(self):
        """Ollama alone allows 120s; other workers time out at 30s."""
        seen = []

        def resolver(text):
            seen.append(self.conn.in_transaction)
            return {"search_query": "q", "target_found": False,
                    "found_url": None, "monitor_urls": [], "reason": "なし"}

        note = FakeNote("n1", text="監視して")
        with mock.patch.object(watch_keep, "resolve_web_monitor", side_effect=resolver):
            self.run_note(note, [item("web_monitor", summary="監視対象")],
                          lambda t, e: True)
        self.assertEqual(seen, [False], "検索中にトランザクションを開いていてはいけない")


class SafetyTimerTests(unittest.TestCase):
    """The fallback check must not depend on the ntfy stream."""

    def test_the_timer_runs_without_any_stream_activity(self):
        calls = []
        stop = threading.Event()
        clock = {"t": 0}

        def sleeper(seconds):
            clock["t"] += seconds
            if len(calls) >= 2:
                stop.set()

        with mock.patch.object(tasks_event_listener, "process_cycle",
                               side_effect=lambda reason: calls.append(reason)):
            tasks_event_listener._last_cycle = 0
            tasks_event_listener.run_safety_checks(
                interval=600, sleeper=sleeper, clock=lambda: clock["t"], stop=stop
            )
        self.assertGreaterEqual(len(calls), 1)
        self.assertIn("ntfy非依存", calls[0])


if __name__ == "__main__":
    unittest.main()
