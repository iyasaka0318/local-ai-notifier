import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from instance_lock import SingleInstanceLock
from state_store import (
    cancel_reminders,
    cancel_web_monitors,
    claim_note,
    ensure_schema,
    list_active_web_monitors,
    list_pending_reminders,
    mark_note_failed,
    mark_note_processed,
    note_content_hash,
    requeue_stale_running_jobs,
    save_structured_item,
    upsert_web_monitor,
    upsert_research_job,
)


def result(intent, summary="summary", calendar_ready=False):
    return {
        "intent": intent,
        "summary": summary,
        "actionable": intent != "memo",
        "calendar_ready": calendar_ready,
        "needs_target_resolution": intent == "web_monitor",
        "needs_confirmation": False,
        "missing_information": [],
    }


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_claim_skips_completed_content_and_retries_failure(self):
        first_hash = note_content_hash("title", "body")
        self.assertTrue(claim_note(self.conn, "n1", first_hash))
        mark_note_processed(self.conn, "n1", first_hash)
        self.conn.commit()
        self.assertFalse(claim_note(self.conn, "n1", first_hash))

        second_hash = note_content_hash("title", "edited")
        self.assertTrue(claim_note(self.conn, "n1", second_hash))
        mark_note_failed(self.conn, "n1", second_hash, "temporary error")
        self.assertTrue(claim_note(self.conn, "n1", second_hash))

    def test_only_stale_running_jobs_are_requeued(self):
        now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
        old = (now - timedelta(hours=1)).isoformat()
        recent = (now - timedelta(minutes=5)).isoformat()
        self.conn.executemany("""
            INSERT INTO wake_jobs (note_id, status, created_at, updated_at)
            VALUES (?, 'running', ?, ?)
        """, (("old", old, old), ("recent", recent, recent)))
        self.conn.commit()

        self.assertEqual(
            requeue_stale_running_jobs(self.conn, "wake_jobs", now=now),
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM wake_jobs WHERE note_id = 'old'"
            ).fetchone()[0],
            "retry",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM wake_jobs WHERE note_id = 'recent'"
            ).fetchone()[0],
            "running",
        )
    def test_structured_items_upsert_and_supersede(self):
        cursor = self.conn.cursor()
        save_structured_item(cursor, "n1", "t", "body", result("todo"))
        self.conn.commit()
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM todos WHERE note_id = 'n1'"
            ).fetchone()[0],
            "pending",
        )

        save_structured_item(
            cursor,
            "n1",
            "t",
            "body edited",
            result("memo", "new summary"),
        )
        self.conn.commit()
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM todos WHERE note_id = 'n1'"
            ).fetchone()[0],
            "superseded",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT summary, status FROM memos WHERE note_id = 'n1'"
            ).fetchone(),
            ("new summary", "active"),
        )

    def test_calendar_job_keeps_readiness_and_missing_information(self):
        calendar = result("calendar", "meeting", calendar_ready=True)
        calendar["missing_information"] = ["location"]
        calendar.update({
            "event_title": "田中さんと会う",
            "event_start": "2026-09-25T15:00:00+09:00",
            "event_end": None,
            "all_day": False,
            "event_location": None,
            "event_description": None,
        })
        save_structured_item(
            self.conn.cursor(), "n2", "meeting", "September 25 15:00", calendar
        )
        self.conn.commit()
        row = self.conn.execute("""
            SELECT calendar_ready, missing_information, status,
                   event_title, event_start
            FROM calendar_jobs WHERE note_id = 'n2'
        """).fetchone()
        self.assertEqual(row, (
            1,
            '["location"]',
            "pending",
            "田中さんと会う",
            "2026-09-25T15:00:00+09:00",
        ))

    def test_reminder_is_pending_only_with_a_scheduled_time(self):
        scheduled = result("reminder", "laundry")
        scheduled["scheduled_at"] = "2026-09-18T08:00:00+09:00"
        scheduled["notification_text"] = "部屋の掃除をする"
        save_structured_item(self.conn.cursor(), "r1", "", "notify", scheduled)

        incomplete = result("reminder", "laundry")
        incomplete["scheduled_at"] = None
        save_structured_item(self.conn.cursor(), "r2", "", "notify", incomplete)
        self.conn.commit()

        self.assertEqual(
            self.conn.execute(
                "SELECT status, summary FROM reminders WHERE note_id = 'r1'"
            ).fetchone(),
            ("pending", "部屋の掃除をする"),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM reminders WHERE note_id = 'r2'"
            ).fetchone()[0],
            "waiting_information",
        )

    def test_notified_reminder_is_not_reset_by_idempotent_retry(self):
        reminder = result("reminder", "laundry")
        reminder["scheduled_at"] = "2026-09-18T08:00:00+09:00"
        cursor = self.conn.cursor()
        save_structured_item(cursor, "r1", "", "notify", reminder)
        cursor.execute("""
            UPDATE reminders
            SET status = 'notified', notified_at = '2026-09-18T08:00:01+09:00'
            WHERE note_id = 'r1'
        """)
        save_structured_item(cursor, "r1", "", "notify", reminder)
        self.conn.commit()
        self.assertEqual(
            self.conn.execute("""
                SELECT status, notified_at FROM reminders WHERE note_id = 'r1'
            """).fetchone(),
            ("notified", "2026-09-18T08:00:01+09:00"),
        )

    def test_pending_reminders_can_be_listed_and_safely_cancelled(self):
        first = result("reminder", "部屋の掃除をする")
        first["scheduled_at"] = "2026-09-20T04:00:00+09:00"
        second = result("reminder", "洗濯物を取り込む")
        second["scheduled_at"] = "2026-09-21T08:00:00+09:00"
        save_structured_item(self.conn.cursor(), "r1", "", "", first)
        save_structured_item(self.conn.cursor(), "r2", "", "", second)
        self.conn.commit()

        active = list_pending_reminders(self.conn)
        self.assertEqual([item["note_id"] for item in active], ["r1", "r2"])
        cancelled = cancel_reminders(self.conn, ["r1", "missing"])
        self.conn.commit()
        self.assertEqual([item["note_id"] for item in cancelled], ["r1"])
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM reminders WHERE note_id = 'r1'"
            ).fetchone()[0],
            "cancelled",
        )
        self.assertEqual(
            [item["note_id"] for item in list_pending_reminders(self.conn)],
            ["r2"],
        )
        self.assertEqual(cancel_reminders(self.conn, ["r1"]), [])

    def test_web_monitor_edit_updates_existing_job(self):
        first = {
            "target_found": False,
            "search_query": "first query",
            "monitor_urls": ["https://example.com/events"],
            "found_url": None,
        }
        second = {
            "target_found": True,
            "search_query": "edited query",
            "monitor_urls": [],
            "found_url": "https://example.com/event/73",
        }
        cursor = self.conn.cursor()
        self.assertTrue(upsert_web_monitor(cursor, "n3", "first", first))
        self.assertFalse(upsert_web_monitor(cursor, "n3", "edited", second))
        self.conn.commit()
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM web_monitors WHERE note_id = 'n3'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute("""
                SELECT request_text, status, found_url
                FROM web_monitors WHERE note_id = 'n3'
            """).fetchone(),
            ("edited", "found", "https://example.com/event/73"),
        )

    def test_web_monitor_list_and_cancel_are_safe_and_idempotent(self):
        resolved = {
            "target_found": False,
            "search_query": "event registration",
            "monitor_urls": ["https://example.com/events"],
            "found_url": None,
        }
        cursor = self.conn.cursor()
        upsert_web_monitor(cursor, "m1", "研究会の受付開始", resolved)
        upsert_web_monitor(cursor, "m2", "展示会のチケット発売", resolved)
        self.conn.commit()

        active = list_active_web_monitors(self.conn)
        self.assertEqual([item["request_text"] for item in active], [
            "研究会の受付開始",
            "展示会のチケット発売",
        ])

        cancelled = cancel_web_monitors(self.conn, [active[1]["id"]])
        self.conn.commit()
        self.assertEqual(cancelled[0]["request_text"], "展示会のチケット発売")
        self.assertEqual(len(list_active_web_monitors(self.conn)), 1)
        self.assertEqual(cancel_web_monitors(self.conn, [active[1]["id"]]), [])

    def test_research_job_is_idempotent_per_source_note(self):
        plan = {
            "objective": "event details",
            "query": "event details official",
            "requested_items": ["price", "place"],
            "execute_at": None,
            "save_to_keep": True,
            "notify_mode": "after_completion",
            "notify_at": None,
            "notification_content_mode": "completion_only",
        }
        cursor = self.conn.cursor()
        first_id = upsert_research_job(cursor, "source-1", plan)
        second_id = upsert_research_job(cursor, "source-1", plan)
        self.conn.commit()
        self.assertEqual(first_id, second_id)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM research_jobs WHERE source_note_id = 'source-1'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT notification_content_mode FROM research_jobs"
            ).fetchone()[0],
            "completion_only",
        )

    def test_legacy_processed_rows_are_backfilled(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE processed_notes (note_id TEXT PRIMARY KEY)")
        conn.execute("""
            CREATE TABLE ai_results (
                note_id TEXT PRIMARY KEY, title TEXT, original_text TEXT,
                intent TEXT, summary TEXT, actionable INTEGER,
                calendar_ready INTEGER, needs_target_resolution INTEGER,
                needs_confirmation INTEGER, missing_information TEXT,
                processed_at TEXT
            )
        """)
        conn.execute("INSERT INTO processed_notes VALUES ('old')")
        conn.execute("""
            INSERT INTO ai_results (note_id, title, original_text)
            VALUES ('old', 'title', 'body')
        """)
        ensure_schema(conn)
        row = conn.execute("""
            SELECT content_hash, status FROM processed_notes WHERE note_id = 'old'
        """).fetchone()
        self.assertEqual(row, (note_content_hash("title", "body"), "processed"))
        conn.close()

    def test_existing_ai_results_populate_new_work_tables(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE processed_notes (note_id TEXT PRIMARY KEY)")
        conn.execute("""
            CREATE TABLE ai_results (
                note_id TEXT PRIMARY KEY, title TEXT, original_text TEXT,
                intent TEXT, summary TEXT, actionable INTEGER,
                calendar_ready INTEGER, needs_target_resolution INTEGER,
                needs_confirmation INTEGER, missing_information TEXT,
                processed_at TEXT
            )
        """)
        conn.execute("INSERT INTO processed_notes VALUES ('old-todo')")
        conn.execute("""
            INSERT INTO ai_results VALUES (
                'old-todo', 'title', 'body', 'todo', 'legacy task', 1,
                0, 0, 0, '[]', '2026-01-01T00:00:00+09:00'
            )
        """)
        ensure_schema(conn)
        self.assertEqual(
            conn.execute("""
                SELECT summary, status FROM todos WHERE note_id = 'old-todo'
            """).fetchone(),
            ("legacy task", "pending"),
        )
        conn.execute(
            "UPDATE todos SET status = 'completed' WHERE note_id = 'old-todo'"
        )
        conn.commit()
        ensure_schema(conn)
        self.assertEqual(
            conn.execute(
                "SELECT status FROM todos WHERE note_id = 'old-todo'"
            ).fetchone()[0],
            "completed",
        )
        conn.close()


class InstanceLockTests(unittest.TestCase):
    def test_second_instance_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "worker.lock")
            first = SingleInstanceLock(path)
            second = SingleInstanceLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()


if __name__ == "__main__":
    unittest.main()
    list_active_web_monitors,
