import sqlite3
import unittest
from unittest.mock import patch

import research_worker
from state_store import ensure_schema, note_content_hash, upsert_research_job, utc_now


class FakeNote:
    def __init__(self, note_id, title="", text=""):
        self.id = note_id
        self.title = title
        self.text = text
        self.trashed = False

    def trash(self):
        self.trashed = True


class FakeKeep:
    def __init__(self):
        self.notes = {}
        self.created = 0
        self.synced = 0

    def get(self, note_id):
        return self.notes.get(note_id)

    def createNote(self, title, text):
        self.created += 1
        note = FakeNote(f"generated-{self.created}", title, text)
        self.notes[note.id] = note
        return note

    def sync(self):
        self.synced += 1


class ResearchWorkerTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def add_job(self, notify_mode="after_completion", save_to_keep=False):
        source_id = "source-1"
        content_hash = note_content_hash("[AI] test", "AIメモ\ntest\nAIメモ")
        now = utc_now()
        self.conn.execute(
            """INSERT INTO processed_notes
               (note_id, content_hash, updated_at, status)
               VALUES (?, ?, ?, 'waiting_downstream')""",
            (source_id, content_hash, now),
        )
        self.conn.execute(
            """INSERT INTO ai_results
               (note_id, title, original_text, intent, summary, actionable,
                calendar_ready, needs_target_resolution, needs_confirmation,
                missing_information, processed_at, scheduled_at,
                automation_source, recurrence, actions)
               VALUES (?, '', '', 'research', '調査', 1, 0, 0, 0, '[]', ?,
                       NULL, 'inferred_automation', NULL, '[]')""",
            (source_id, now),
        )
        job_id = upsert_research_job(self.conn.cursor(), source_id, {
            "objective": "調査する",
            "query": "調査する",
            "requested_items": ["結果"],
            "execute_at": None,
            "save_to_keep": save_to_keep,
            "notify_mode": notify_mode,
            "notify_at": None,
            "notification_content_mode": "result_summary",
        })
        self.conn.commit()
        return self.conn.execute("""
            SELECT id, source_note_id, objective, query, requested_items, execute_at,
                   save_to_keep, notify_mode, notify_at, result_text,
                   memo_title, memo_text, notification_title, completion_text,
                   notification_text, notification_detailed_text,
                   notification_content_mode,
                   generated_note_id, notified_at
            FROM research_jobs WHERE id = ?
        """, (job_id,)).fetchone()

    def test_research_completion_notifies_and_finalizes_source(self):
        job = self.add_job()
        sent = []
        output = {
            "result_text": "調査結果の詳細です。",
            "memo_title": "調査結果",
            "memo_text": "■ 結果\n調査結果の詳細です。",
            "notification_title": "調査結果",
            "completion_text": "調査が完了しました。",
            "notification_summary": "調査結果の要点です。",
            "notification_detailed": "調査結果の詳細です。",
            "unresolved_items": [],
        }
        with patch.object(
            research_worker, "run_research", return_value=(output, ["https://example.com"])
        ), patch.object(
            research_worker, "get_keep", return_value=FakeKeep()
        ), patch.object(
            research_worker, "send_notification", side_effect=lambda topic, text, title: sent.append((topic, text, title))
        ):
            self.assertTrue(research_worker.process_job(self.conn, job, "topic"))

        self.assertEqual(
            sent,
            [("topic", "調査結果の要点です。", "調査結果")],
        )
        self.assertEqual(
            self.conn.execute("SELECT status FROM research_jobs").fetchone()[0],
            "completed",
        )
        self.assertEqual(
            self.conn.execute("SELECT status FROM processed_notes").fetchone()[0],
            "processed",
        )

    def test_generated_keep_note_is_idempotent(self):
        job = self.add_job(notify_mode="none", save_to_keep=True)
        keep = FakeKeep()
        first = research_worker.save_result_to_keep(
            self.conn, keep, job[0], "目的", "結果"
        )
        second = research_worker.save_result_to_keep(
            self.conn, keep, job[0], "目的", "結果"
        )

        self.assertEqual(first, second)
        self.assertEqual(keep.created, 1)
        self.assertEqual(keep.notes[first].title, "[AI結果] 目的")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM generated_notes").fetchone()[0],
            1,
        )

    def test_notification_text_is_selected_per_mode(self):
        self.assertEqual(
            research_worker.select_notification_text(
                "completion_only", "完了しました。", "要約", "詳細", False
            ),
            "完了しました。",
        )
        self.assertEqual(
            research_worker.select_notification_text(
                "result_summary", "完了", "要約", "詳細", True
            ),
            "要約\n\n詳細はGoogle Keepに保存しました。",
        )
        self.assertEqual(
            research_worker.select_notification_text(
                "detailed_result", "完了", "要約", "詳細", False
            ),
            "詳細",
        )

    def test_private_page_target_is_rejected(self):
        with self.assertRaises(ValueError):
            research_worker.validate_public_url("http://127.0.0.1/private")

    def test_english_output_is_rewritten_before_delivery(self):
        english = {
            "result_text": "Detailed research result",
            "memo_title": "Research result",
            "memo_text": "Detailed memo text",
            "notification_title": "Research completed",
            "completion_text": "Research completed",
            "notification_summary": "Short research summary",
            "notification_detailed": "Detailed notification text",
            "unresolved_items": [],
        }
        japanese = {
            "result_text": "詳細な調査結果です。",
            "memo_title": "調査結果",
            "memo_text": "■ 結果\n詳細な調査結果です。",
            "notification_title": "調査完了",
            "completion_text": "調査が完了しました。",
            "notification_summary": "調査結果の要点です。",
            "notification_detailed": "調査結果の詳細です。",
            "unresolved_items": [],
        }
        with patch.object(research_worker, "ask_ollama", return_value=japanese):
            result = research_worker.ensure_japanese_research_output(english)
        self.assertEqual(result["memo_title"], "調査結果")
        self.assertEqual(result["notification_title"], "調査完了")


if __name__ == "__main__":
    unittest.main()
