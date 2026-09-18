import sqlite3
import unittest
from unittest import mock

import calendar_worker
from action_log import record_action, undo_action
from state_store import (
    ensure_schema,
    has_open_remote_delete,
    open_remote_deletes,
    utc_now,
)


class CancelDuringCreationTests(unittest.TestCase):
    """A cancellation that lands while Google is creating the event."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ensure_schema(self.conn)
        now = utc_now()
        self.conn.execute("""
            INSERT INTO calendar_jobs (
                note_id, summary, calendar_ready, missing_information, status,
                event_title, event_start, created_at, updated_at
            ) VALUES ('c1', '歯医者', 1, '[]', 'pending',
                      '歯医者', '2026-10-03T09:00:00+09:00', ?, ?)
        """, (now, now))
        self.conn.execute(
            "INSERT INTO processed_notes (note_id, content_hash, status, revision)"
            " VALUES ('c1', 'h', 'waiting_downstream', 1)"
        )
        self.token = record_action(
            self.conn, source_note_id="c1", item_id="c1",
            kind="calendar", summary="歯医者", note_revision=1,
        )
        self.conn.commit()
        self.deleted = []

    def tearDown(self):
        self.conn.close()

    def job_row(self):
        return self.conn.execute("""
            SELECT note_id, event_title, event_start, event_end, all_day,
                   event_location, event_description FROM calendar_jobs
            WHERE note_id = 'c1'
        """).fetchone()

    def status(self):
        return self.conn.execute(
            "SELECT status FROM calendar_jobs WHERE note_id = 'c1'"
        ).fetchone()[0]

    def test_a_cancel_mid_creation_is_not_overwritten(self):
        def creator_that_gets_cancelled(credentials, body, session=None):
            undo_action(self.conn, self.token)
            self.conn.commit()
            return {"id": "evt-1"}

        with mock.patch.object(calendar_worker, "delete_calendar_event",
                               side_effect=lambda i, e: self.deleted.append((i, e))):
            calendar_worker.process_calendar_job(
                self.conn, self.job_row(),
                webhook_credentials_factory=lambda: {"endpoint_url": "u", "secret": "s"},
                webhook_event_creator=creator_that_gets_cancelled,
            )

        self.assertEqual(self.status(), "cancelled", "取消がcreatedで上書きされてはいけない")
        self.assertEqual(self.deleted, [("c1", "evt-1")], "作成済み予定を後追い削除する")

    def test_a_job_cancelled_before_the_claim_is_skipped(self):
        undo_action(self.conn, self.token)
        self.conn.commit()
        calls = []
        calendar_worker.process_calendar_job(
            self.conn, self.job_row(),
            webhook_credentials_factory=lambda: {"endpoint_url": "u", "secret": "s"},
            webhook_event_creator=lambda c, b, session=None: calls.append(b) or {"id": "x"},
        )
        self.assertEqual(calls, [], "取消済みのジョブはGoogleへ送らない")
        self.assertEqual(self.status(), "cancelled")

    def test_a_failed_delete_stays_queued_for_retry(self):
        def creator_that_gets_cancelled(credentials, body, session=None):
            undo_action(self.conn, self.token)
            self.conn.commit()
            return {"id": "evt-1"}

        def offline(item_id, remote_id):
            raise RuntimeError("offline")

        with mock.patch.object(calendar_worker, "delete_calendar_event", side_effect=offline):
            calendar_worker.process_calendar_job(
                self.conn, self.job_row(),
                webhook_credentials_factory=lambda: {"endpoint_url": "u", "secret": "s"},
                webhook_event_creator=creator_that_gets_cancelled,
            )

        self.assertTrue(has_open_remote_delete(self.conn, "calendar", "c1"))
        pending = open_remote_deletes(self.conn, "calendar")
        self.assertEqual(pending[0]["remote_id"], "evt-1")
        self.assertEqual(pending[0]["attempts"], 1)

        # The next worker pass drains it.
        with mock.patch.object(calendar_worker, "delete_calendar_event",
                               side_effect=lambda i, e: self.deleted.append((i, e))):
            calendar_worker.drain_remote_deletes(
                self.conn, "calendar", calendar_worker.delete_calendar_event
            )
        self.assertEqual(self.deleted, [("c1", "evt-1")])
        self.assertFalse(has_open_remote_delete(self.conn, "calendar", "c1"))


class RetappingAfterAFailedDeleteTests(unittest.TestCase):
    """The second tap has to retry the remote delete, not report success."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ensure_schema(self.conn)
        now = utc_now()
        self.conn.execute("""
            INSERT INTO calendar_jobs (
                note_id, summary, calendar_ready, missing_information, status,
                calendar_event_id, created_at, updated_at
            ) VALUES ('c1', '歯医者', 1, '[]', 'created', 'evt-1', ?, ?)
        """, (now, now))
        self.conn.execute(
            "INSERT INTO processed_notes (note_id, content_hash, status, revision)"
            " VALUES ('c1', 'h', 'processed', 1)"
        )
        self.token = record_action(
            self.conn, source_note_id="c1", item_id="c1",
            kind="calendar", summary="歯医者", note_revision=1,
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_the_second_tap_reports_the_outstanding_delete(self):
        undo_action(self.conn, self.token)
        self.conn.commit()
        self.assertTrue(has_open_remote_delete(self.conn, "calendar", "c1"))

        ok, message = undo_action(self.conn, self.token)
        self.assertTrue(ok)
        self.assertIn("削除が未完了", message)

    def test_draining_twice_only_deletes_once(self):
        undo_action(self.conn, self.token)
        self.conn.commit()
        calls = []
        from state_store import drain_remote_deletes
        drain_remote_deletes(self.conn, "calendar", lambda i, e: calls.append((i, e)))
        drain_remote_deletes(self.conn, "calendar", lambda i, e: calls.append((i, e)))
        self.assertEqual(calls, [("c1", "evt-1")])


if __name__ == "__main__":
    unittest.main()
