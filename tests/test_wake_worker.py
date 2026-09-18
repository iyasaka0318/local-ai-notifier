import sqlite3
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from persistent_reminders import add_task
from state_store import ensure_schema, note_content_hash
from wake_worker import build_weather_notification, process_wake_job


JST = ZoneInfo("Asia/Tokyo")


def sample_weather(current_code=0, future_rain=False):
    probabilities = [10, 10, 10, 10]
    codes = [0, 0, 0, 0]
    precipitation = [0, 0, 0, 0]
    if future_rain:
        probabilities[2] = 50
        codes[2] = 61
        precipitation[2] = 0.5
    return {
        "current": {
            "temperature_2m": 22.5,
            "precipitation": 0.5 if current_code == 61 else 0,
            "rain": 0.5 if current_code == 61 else 0,
            "weather_code": current_code,
        },
        "hourly": {
            "time": [
                "2026-09-18T10:00",
                "2026-09-18T11:00",
                "2026-09-18T12:00",
                "2026-09-18T13:00",
            ],
            "temperature_2m": [22.5, 23.0, 23.5, 24.0],
            "precipitation_probability": probabilities,
            "precipitation": precipitation,
            "rain": precipitation,
            "weather_code": codes,
        },
    }


class WakeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ensure_schema(self.conn)
        content_hash = note_content_hash("[AI] 起床", "起きた")
        self.conn.execute("""
            INSERT INTO processed_notes (
                note_id, content_hash, updated_at, status
            ) VALUES ('wake-note', ?, 'now', 'waiting_downstream')
        """, (content_hash,))
        self.conn.execute("""
            INSERT INTO ai_results (
                note_id, intent, summary, actionable, calendar_ready,
                needs_target_resolution, needs_confirmation,
                missing_information, processed_at, automation_source
            ) VALUES (
                'wake-note', 'wake_briefing', '起床', 1, 0, 0, 0,
                '[]', 'now', 'inferred_automation'
            )
        """)
        self.conn.execute("""
            INSERT INTO wake_jobs (note_id, status, created_at, updated_at)
            VALUES ('wake-note', 'pending', 'now', 'now')
        """)
        add_task(
            self.conn, "task-a", "バイト先にお菓子を買う",
            now="2026-09-18T09:00:00+09:00",
        )
        add_task(
            self.conn, "task-b", "郵便物を出す",
            now="2026-09-18T09:00:00+09:00",
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_weather_mentions_future_rain_probability(self):
        text = build_weather_notification(
            sample_weather(future_rain=True),
            datetime(2026, 9, 18, 10, 0, tzinfo=JST),
        )
        self.assertIn("現在は晴れ", text)
        self.assertIn("12時ごろから降水", text)
        self.assertIn("最大降水確率50%", text)

    def test_wake_notifications_are_separate_ordered_and_idempotent(self):
        sent = []
        events = [{
            "id": "event-1",
            "title": "田中さんと会う",
            "all_day": False,
            "start": "2026-09-18T06:00:00.000Z",
            "end": "2026-09-18T07:00:00.000Z",
            "location": "前橋駅",
        }]
        now = datetime(2026, 9, 18, 10, 0, tzinfo=JST)

        process_wake_job(
            self.conn,
            "wake-note",
            "topic",
            now=now,
            weather_fetcher=lambda: sample_weather(),
            calendar_fetcher=lambda _now: events,
            sender=lambda topic, message, title: sent.append((title, message)),
            keep_factory=lambda: None,
        )
        process_wake_job(
            self.conn,
            "wake-note",
            "topic",
            now=now,
            weather_fetcher=lambda: sample_weather(),
            calendar_fetcher=lambda _now: events,
            sender=lambda topic, message, title: sent.append((title, message)),
            keep_factory=lambda: None,
        )

        self.assertEqual(
            [title for title, _message in sent],
            ["起床後の天気", "24時間以内の予定", "リマインダー", "リマインダー"],
        )
        self.assertIn("田中さんと会う", sent[1][1])
        self.assertEqual(sent[2][1], "バイト先にお菓子を買う")
        self.assertEqual(sent[3][1], "郵便物を出す")
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM wake_jobs WHERE note_id = 'wake-note'"
            ).fetchone()[0],
            "completed",
        )


if __name__ == "__main__":
    unittest.main()
