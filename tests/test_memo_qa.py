import unittest

import memo_qa
from state_store import ensure_schema


def insert_ai_result(conn, note_id, text, intent="memo", summary="", processed_at="2026-09-20T10:00:00"):
    conn.execute(
        "INSERT INTO ai_results (note_id, title, original_text, intent, summary, processed_at) VALUES (?, '', ?, ?, ?, ?)",
        (note_id, text, intent, summary, processed_at),
    )


class TestFitBudget(unittest.TestCase):
    def test_fit_budget_keeps_prefix_in_order(self):
        records = [{"id": "a", "text": "x" * 100} for _ in range(3)]
        kept = memo_qa.fit_budget(records, budget=250)
        self.assertEqual([r["id"] for r in kept], ["a", "a"])


class TestCalendarRecords(unittest.TestCase):
    def test_calendar_records_converts_timed_event_to_local_time(self):
        events = [{
            "title": "歯医者",
            "all_day": False,
            "start": "2026-10-03T01:00:00.000Z",
            "end": "2026-10-03T02:00:00.000Z",
            "location": "駅前",
        }]
        records = memo_qa.calendar_records(events)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["id"], "calendar-0")
        self.assertEqual(record["kind"], "カレンダー")
        self.assertEqual(record["text"], "歯医者")
        self.assertEqual(record["start"], "2026-10-03 10:00")
        self.assertEqual(record["location"], "駅前")

    def test_calendar_records_keeps_all_day_start_as_is(self):
        events = [{"title": "旅行", "all_day": True, "start": "2026-10-10", "end": "2026-10-12"}]
        records = memo_qa.calendar_records(events)
        record = records[0]
        self.assertEqual(record["start"], "2026-10-10")
        self.assertTrue(record["all_day"])


class TestLocalRecords(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3_connect()

    def test_local_records_excludes_question_intent(self):
        insert_ai_result(self.conn, "n1", "メモ本文", intent="memo")
        insert_ai_result(self.conn, "n2", "質問本文", intent="question")
        records = memo_qa.local_records(self.conn)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], "n1")
        self.assertEqual(records[0]["kind"], "メモ")

    def test_local_records_orders_by_processed_at_desc(self):
        insert_ai_result(self.conn, "old", "古い", processed_at="2026-01-01T00:00:00")
        insert_ai_result(self.conn, "new", "新しい", processed_at="2026-09-01T00:00:00")
        records = memo_qa.local_records(self.conn)
        self.assertEqual([r["id"] for r in records], ["new", "old"])


class TestFormatSources(unittest.TestCase):
    def test_format_sources_renders_known_ids_and_skips_unknown(self):
        records = [{"id": "n1", "kind": "メモ", "recorded_at": "2026-09-20 10:00", "text": "駐車場は3階"}]
        lines = memo_qa.format_sources(["n1", "missing"], records)
        self.assertEqual(lines, ["・メモ 2026-09-20「駐車場は3階」"])


class TestAnswerQuestion(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3_connect()
        insert_ai_result(self.conn, "n1", "駐車場は3階", intent="memo")

    def test_answer_question_found_includes_sources(self):
        def fake_ask(prompt, payload, schema):
            return {"found": True, "answer": "3階です。", "source_ids": ["n1"]}

        def fake_fetch_events(now):
            return []

        result = memo_qa.answer_question(self.conn, "駐車場はどこ？", ask=fake_ask, fetch_events=fake_fetch_events)
        self.assertTrue(result["found"])
        self.assertTrue(result["text"].startswith("3階です。"))
        self.assertIn("根拠:", result["text"])
        self.assertIn("駐車場は3階", result["text"])

    def test_answer_question_not_found_has_no_sources(self):
        def fake_ask(prompt, payload, schema):
            return {"found": False, "answer": "記録には見当たりません。", "source_ids": ["n1"]}

        def fake_fetch_events(now):
            return []

        result = memo_qa.answer_question(self.conn, "存在しない質問？", ask=fake_ask, fetch_events=fake_fetch_events)
        self.assertFalse(result["found"])
        self.assertNotIn("根拠:", result["text"])

    def test_answer_question_calendar_failure_sets_calendar_available_false(self):
        captured = {}

        def fake_ask(prompt, payload, schema):
            captured["payload"] = payload
            return {"found": False, "answer": "記録には見当たりません。", "source_ids": []}

        def fake_fetch_events(now):
            raise RuntimeError("カレンダー取得失敗")

        result = memo_qa.answer_question(self.conn, "予定は？", ask=fake_ask, fetch_events=fake_fetch_events)
        self.assertFalse(result["found"])
        self.assertFalse(captured["payload"]["calendar_available"])

    def test_answer_question_passes_question_and_records_to_ask(self):
        captured = {}

        def fake_ask(prompt, payload, schema):
            captured["payload"] = payload
            return {"found": False, "answer": "記録には見当たりません。", "source_ids": []}

        def fake_fetch_events(now):
            return []

        memo_qa.answer_question(self.conn, "駐車場はどこ？", ask=fake_ask, fetch_events=fake_fetch_events)
        payload = captured["payload"]
        self.assertEqual(payload["question"], "駐車場はどこ？")
        ids = [r["id"] for r in payload["records"]]
        self.assertIn("n1", ids)


def sqlite3_connect():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    return conn


if __name__ == "__main__":
    unittest.main()
