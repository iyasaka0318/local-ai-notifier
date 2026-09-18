import sqlite3
import unittest
from datetime import timedelta
from unittest.mock import Mock

import calendar_worker
from state_store import ensure_schema, note_content_hash, save_structured_item, utc_now


class CalendarWorkerTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_all_day_event_uses_title_without_date_or_command(self):
        body = calendar_worker.build_event_body(
            "note-1",
            "高校の同期の飲み会",
            "2026-09-20",
            all_day=True,
        )
        self.assertEqual(body["summary"], "高校の同期の飲み会")
        self.assertEqual(body["start"], {"date": "2026-09-20"})
        self.assertEqual(body["end"], {"date": "2026-09-21"})
        self.assertEqual(body["id"], calendar_worker.deterministic_event_id("note-1"))

    def test_timed_event_gets_default_duration(self):
        body = calendar_worker.build_event_body(
            "note-2",
            "田中さんと会う",
            "2026-09-20T15:00:00+09:00",
        )
        start = calendar_worker.parse_scheduled_at(body["start"]["dateTime"])
        end = calendar_worker.parse_scheduled_at(body["end"]["dateTime"])
        self.assertEqual(end - start, timedelta(minutes=60))

    def test_conflict_updates_the_same_deterministic_event(self):
        class Response:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self.payload = payload

            def raise_for_status(self):
                if self.status_code >= 400 and self.status_code != 409:
                    raise RuntimeError(self.status_code)

            def json(self):
                return self.payload

        class Session:
            def __init__(self):
                self.patched = None

            def post(self, *args, **kwargs):
                return Response(409, {})

            def patch(self, *args, **kwargs):
                self.patched = kwargs["json"]
                return Response(200, kwargs["json"])

        session = Session()
        body = calendar_worker.build_event_body(
            "same-note", "修正後の予定", "2026-09-21", all_day=True
        )
        result = calendar_worker.create_calendar_event(
            "token", "primary", body, session=session
        )
        self.assertEqual(result["summary"], "修正後の予定")
        self.assertNotIn("id", session.patched)
        self.assertEqual(result["id"], body["id"])

    def test_success_marks_job_created_and_source_processed(self):
        note_id = "calendar-source"
        now = utc_now()
        content_hash = note_content_hash("[AI] 予定", "明後日に田中さんと会う")
        self.conn.execute("""
            INSERT INTO processed_notes (note_id, content_hash, updated_at, status)
            VALUES (?, ?, ?, 'waiting_downstream')
        """, (note_id, content_hash, now))
        self.conn.execute("""
            INSERT INTO ai_results (
                note_id, title, original_text, intent, summary, actionable,
                calendar_ready, needs_target_resolution, needs_confirmation,
                missing_information, processed_at, automation_source
            ) VALUES (?, '', '', 'calendar', '田中さんと会う', 1, 1, 0, 0,
                      '[]', ?, 'inferred_automation')
        """, (note_id, now))
        save_structured_item(self.conn.cursor(), note_id, "", "", {
            "intent": "calendar",
            "summary": "田中さんと会う",
            "calendar_ready": True,
            "missing_information": [],
            "event_title": "田中さんと会う",
            "event_start": "2026-09-20",
            "event_end": None,
            "all_day": True,
            "event_location": None,
            "event_description": None,
        })
        self.conn.commit()
        job = self.conn.execute("""
            SELECT note_id, event_title, event_start, event_end, all_day,
                   event_location, event_description
            FROM calendar_jobs WHERE note_id = ?
        """, (note_id,)).fetchone()
        keep_factory = Mock(side_effect=AssertionError("Keepは不要"))

        calendar_worker.process_calendar_job(
            self.conn,
            job,
            token_factory=lambda: "token",
            event_creator=lambda token, calendar_id, body: {"id": body["id"]},
            keep_factory=keep_factory,
            webhook_credentials_factory=lambda: None,
        )

        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM calendar_jobs WHERE note_id = ?", (note_id,)
            ).fetchone()[0],
            "created",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM processed_notes WHERE note_id = ?", (note_id,)
            ).fetchone()[0],
            "processed",
        )
        keep_factory.assert_not_called()

    def test_webhook_payload_uses_calendar_fields(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True, "event_id": "apps-script-event"}

        class Session:
            def __init__(self):
                self.payload = None

            def post(self, _url, json, timeout):
                self.payload = json
                self.timeout = timeout
                return Response()

        body = calendar_worker.build_event_body(
            "note-webhook", "田中さんと会う", "2026-09-20", all_day=True
        )
        session = Session()
        result = calendar_worker.create_calendar_event_via_webhook(
            {"endpoint_url": "https://script.google.com/macros/s/example/exec", "secret": "secret"},
            body,
            session=session,
        )

        self.assertEqual(result["id"], "apps-script-event")
        self.assertEqual(session.payload["source_note_id"], "note-webhook")
        self.assertEqual(session.payload["event_title"], "田中さんと会う")
        self.assertEqual(session.payload["event_start"], "2026-09-20")
        self.assertEqual(session.payload["event_end"], "2026-09-21")
        self.assertTrue(session.payload["all_day"])


if __name__ == "__main__":
    unittest.main()
