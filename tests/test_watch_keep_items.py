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
        "correction_text": None,
        "reference_target": None,
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


class SparseOutputTests(unittest.TestCase):
    """The model omits empty fields; the rest of the pipeline must not notice."""

    def test_omitted_fields_are_restored_with_their_empty_values(self):
        items = watch_keep.normalize_items({"items": [
            {"intent": "memo", "summary": "駐車場は3階"},
        ]})
        item = items[0]
        self.assertEqual(set(item), set(watch_keep.ITEM_SCHEMA["properties"]))
        self.assertIsNone(item["scheduled_at"])
        self.assertIs(item["calendar_ready"], False)
        self.assertEqual(item["actions"], [])

    def test_written_fields_survive(self):
        items = watch_keep.normalize_items({"items": [{
            "intent": "calendar", "summary": "面談", "calendar_ready": True,
            "event_start": "2026-10-06T15:00:00+09:00",
        }]})
        self.assertIs(items[0]["calendar_ready"], True)
        self.assertEqual(items[0]["event_start"], "2026-10-06T15:00:00+09:00")

    def test_defaults_are_not_shared_between_items(self):
        first, second = watch_keep.normalize_items({"items": [
            {"intent": "memo", "summary": "a"}, {"intent": "memo", "summary": "b"},
        ]})
        first["actions"].append("x")
        self.assertEqual(second["actions"], [])


class QuestionTests(MultiItemNoteTests):
    """A question is answered from the records and creates nothing."""

    def setUp(self):
        super().setUp()
        self.sent = []
        self.asked = []
        for name, replacement in (
            ("send_notification",
             lambda topic, message, title="": self.sent.append((title, message))),
            ("answer_question",
             lambda conn, question: self.asked.append(question)
             or {"found": True, "text": "3階のBの12番です。"}),
        ):
            patcher = mock.patch.object(watch_keep, name, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_answer_is_sent_and_nothing_is_recorded_as_a_memo(self):
        note = FakeNote("q1", text="駐車場どこって言ったっけ")
        self.assertTrue(self.run_note(note, [
            item("question", summary="駐車場はどこだと言ったか"),
        ]))
        self.assertEqual(self.sent, [("メモへの回答", "3階のBの12番です。")])
        self.assertEqual(self.rows("memos"), [])
        self.assertEqual(self.reports, [])

    def test_a_lone_question_is_asked_in_the_users_own_words(self):
        note = FakeNote("q1", text="駐車場どこって言ったっけ")
        self.run_note(note, [item("question", summary="駐車場はどこだと言ったか")])
        self.assertEqual(self.asked, ["駐車場どこって言ったっけ"])

    def test_a_question_beside_another_request_uses_its_summary(self):
        note = FakeNote("q1", text="今日の予定なんだっけ、あと牛乳買うの覚えといて")
        self.run_note(note, [
            item("question", summary="今日の予定は何か"),
            item("persistent_reminder", summary="牛乳を買う",
                 persistent_reminder_action="add", persistent_task_text="牛乳を買う"),
        ])
        self.assertEqual(self.asked, ["今日の予定は何か"])
        self.assertEqual([e["kind"] for e in self.reports[0]], ["persistent_reminder"])

    def test_a_failed_answer_leaves_the_note_for_retry(self):
        note = FakeNote("q1", text="駐車場どこって言ったっけ")
        with mock.patch.object(
            watch_keep, "answer_question", side_effect=RuntimeError("モデル停止")
        ), mock.patch.object(watch_keep, "notify_processing_failure"):
            self.assertFalse(self.run_note(note, [
                item("question", summary="駐車場はどこだと言ったか"),
            ]))
        self.assertEqual(self.sent, [])
        status = self.conn.execute(
            "SELECT status FROM processed_notes WHERE note_id = 'q1'"
        ).fetchone()
        self.assertEqual(status, ("failed",))


class MonitorResolutionSearchTests(unittest.TestCase):
    def test_a_query_without_hits_does_not_discard_the_other_queries(self):
        class FakeSearch:
            def text(self, query, max_results=8):
                if query.startswith('"'):
                    raise RuntimeError("No results found.")
                return [{"title": "公式", "href": "https://example.org/", "body": ""}]

        with mock.patch.object(watch_keep, "DDGS", FakeSearch), mock.patch.object(
            watch_keep, "generate_search_queries",
            return_value=["第73回 参加登録", '"第73回" 参加登録'],
        ), mock.patch.object(watch_keep, "ask_llm", return_value={
            "target_found": False, "target_result_id": None,
            "monitor_result_ids": [0], "reason": "まだ公開されていない",
        }):
            resolved = watch_keep.resolve_web_monitor("第73回の参加登録を見張って")

        self.assertEqual(resolved["monitor_urls"], ["https://example.org/"])
        self.assertEqual(resolved["search_query"], "第73回 参加登録")

    def test_no_hits_at_all_registers_a_search_only_watch(self):
        class EmptySearch:
            def text(self, query, max_results=8):
                raise RuntimeError("No results found.")

        with mock.patch.object(watch_keep, "DDGS", EmptySearch), mock.patch.object(
            watch_keep, "generate_search_queries", return_value=["a", "b"],
        ):
            resolved = watch_keep.resolve_web_monitor("見張って")

        self.assertFalse(resolved["target_found"])
        self.assertEqual(resolved["monitor_urls"], [])


class ReferenceTests(MultiItemNoteTests):
    """「さっきの〜」: a later request builds on, or amends, an earlier one."""

    RESOLVED = {
        "search_query": "q", "target_found": False, "found_url": None,
        "monitor_urls": ["https://example.org/"], "reason": "",
    }

    def setUp(self):
        super().setUp()
        self.resolved_for = []
        patcher = mock.patch.object(
            watch_keep, "resolve_web_monitor",
            side_effect=lambda text: self.resolved_for.append(text) or dict(self.RESOLVED),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def register(self, note_id, text, *items):
        self.run_note(FakeNote(note_id, text=text), list(items))
        return self.reports[-1][0]["token"]

    def test_recent_actions_carry_the_words_the_user_spoke(self):
        self.register("w1", "第73回の参加登録が始まったら通知",
                      item("web_monitor", summary="第73回の参加登録を監視"))
        entry = watch_keep.recent_actions(self.conn)[0]
        self.assertEqual(entry["kind"], "web_monitor")
        self.assertEqual(entry["request"], "第73回の参加登録が始まったら通知")

    def test_a_follow_up_names_what_it_referred_to_in_the_report(self):
        token = self.register("w1", "第73回の参加登録が始まったら通知",
                              item("web_monitor", summary="第73回の参加登録を監視"))
        self.run_note(FakeNote("r1", text="さっきの監視のやつ今あるか調べて"), [
            item("research", summary="第73回の参加登録が始まっているか調べる",
                 reference_target=token),
        ])
        self.assertEqual(self.reports[-1][0]["detail"], "「第73回の参加登録を監視」を参照")

    def test_an_unknown_reference_is_ignored(self):
        self.run_note(FakeNote("r1", text="さっきのやつ調べて"), [
            item("research", summary="調べる", reference_target="no-such-token"),
        ])
        self.assertIsNone(self.reports[-1][0]["detail"])

    def test_a_watch_can_be_retargeted(self):
        token = self.register("w1", "第73回の参加登録が始まったら通知",
                              item("web_monitor", summary="第73回の参加登録を監視"))
        self.run_note(FakeNote("c1", text="さっきの監視、発表申込の方にして"), [
            item("correction", summary="監視対象を発表申込に変更する",
                 correction_target=token, correction_action="rewrite",
                 correction_text="第73回の発表申込が始まったら通知する"),
        ])
        # The new search is made for the corrected request, not for the
        # sentence that asked for the correction.
        self.assertEqual(self.resolved_for[-1], "第73回の発表申込が始まったら通知する")
        monitors = self.conn.execute(
            "SELECT note_id, request_text, status FROM web_monitors"
        ).fetchall()
        self.assertEqual(monitors, [("w1", "第73回の発表申込が始まったら通知する", "active")])

    def test_an_addition_is_appended_and_visible_to_questions(self):
        token = self.register("m1", "駐車場は3階のBの12番",
                              item("memo", summary="駐車場は3階のBの12番"))
        self.run_note(FakeNote("c1", text="この前のメモに、ゲートは北口って足しといて"), [
            item("correction", summary="駐車場のメモに追記する",
                 correction_target=token, correction_action="append",
                 correction_text="ゲートは北口"),
        ])
        summary = self.conn.execute(
            "SELECT summary FROM memos WHERE note_id = 'm1'"
        ).fetchone()[0]
        self.assertEqual(summary, "駐車場は3階のBの12番\nゲートは北口")
        recorded = self.conn.execute(
            "SELECT summary, original_text FROM ai_results WHERE note_id = 'm1'"
        ).fetchone()
        self.assertEqual(recorded, (
            "駐車場は3階のBの12番\nゲートは北口", "駐車場は3階のBの12番\nゲートは北口",
        ))

    def test_a_rewrite_reaches_the_record_questions_read(self):
        token = self.register("m1", "駐車場は3階", item("memo", summary="駐車場は3階"))
        self.run_note(FakeNote("c1", text="さっきのメモ、3階じゃなくて4階"), [
            item("correction", summary="メモを直す", correction_target=token,
                 correction_action="rewrite", correction_text="駐車場は4階"),
        ])
        self.assertEqual(self.conn.execute(
            "SELECT summary FROM ai_results WHERE note_id = 'm1'"
        ).fetchone()[0], "駐車場は4階")


class ResearchDeliveryTests(unittest.TestCase):
    def plan(self, request, actions):
        return watch_keep.build_research_plan(
            {"summary": "調べる", "actions": actions}, request
        )

    def test_a_bare_request_is_answered_by_notification(self):
        research = {"type": "research", "objective": "天気", "requested_items": []}
        self.assertEqual(
            self.plan("群馬の今週末の天気を調べて", [research])["notify_mode"],
            "after_completion",
        )

    def test_saving_to_keep_alone_does_not_add_a_notification(self):
        actions = [
            {"type": "research", "objective": "天気", "requested_items": []},
            {"type": "save_memo"},
        ]
        self.assertEqual(self.plan("天気を調べてメモに残して", actions)["notify_mode"], "none")


class AlarmTests(MultiItemNoteTests):
    def setUp(self):
        super().setUp()
        self.sent = []
        patcher = mock.patch.object(
            watch_keep, "dispatch_phone_commands",
            side_effect=lambda conn, topic: self.sent.append(topic) or 1,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def command(self):
        return self.conn.execute(
            "SELECT note_id, action, payload, status FROM phone_commands"
        ).fetchone()

    def test_an_alarm_is_queued_for_the_phone_and_dispatched(self):
        self.run_note(FakeNote("a1", text="7時にゴミ出しでアラーム"), [
            item("alarm", summary="7時にアラーム", notification_text="ゴミ出し",
                 scheduled_at="2099-01-01T07:00:00+09:00"),
        ])
        note_id, action, payload, status = self.command()
        self.assertEqual((note_id, action, status), ("a1", "alarm_add", "pending"))
        self.assertIn('"seconds": 25200', payload)
        self.assertIn("ゴミ出し", payload)
        self.assertEqual(len(self.sent), 1)
        entry = self.reports[0][0]
        self.assertEqual((entry["kind"], entry["summary"]), ("alarm", "ゴミ出し"))
        self.assertIn("前日にスマホへ登録", entry["detail"])

    def test_a_repeating_alarm_becomes_a_repeating_reminder(self):
        self.run_note(FakeNote("a1", text="毎朝7時にアラーム"), [
            item("alarm", summary="毎朝7時に起きる", notification_text="起きる",
                 scheduled_at="2099-01-01T07:00:00+09:00", recurrence="daily"),
        ])
        self.assertIsNone(self.command())
        self.assertEqual(self.conn.execute(
            "SELECT recurrence FROM reminders WHERE note_id = 'a1'"
        ).fetchone(), ("daily",))
        self.assertIn("繰り返しのアラーム", self.reports[0][0]["fallback_reason"])

    def test_an_alarm_without_a_time_is_kept_as_a_task(self):
        self.run_note(FakeNote("a1", text="アラームかけて"), [
            item("alarm", summary="アラームをかける"),
        ])
        self.assertIsNone(self.command())
        self.assertIn("アラームの時刻", self.reports[0][0]["fallback_reason"])

    def test_undo_before_sending_cancels_the_command(self):
        self.run_note(FakeNote("a1", text="アラーム"), [
            item("alarm", summary="アラーム", scheduled_at="2099-01-01T07:00:00+09:00"),
        ])
        ok, _message = watch_keep.undo_action(self.conn, self.reports[0][0]["token"])
        self.assertTrue(ok)
        self.assertEqual(self.command()[3], "cancelled")

    def test_undo_after_sending_says_to_delete_it_on_the_phone(self):
        self.run_note(FakeNote("a1", text="アラーム"), [
            item("alarm", summary="アラーム", scheduled_at="2099-01-01T07:00:00+09:00"),
        ])
        self.conn.execute("UPDATE phone_commands SET status = 'sent'")
        ok, message = watch_keep.undo_action(self.conn, self.reports[0][0]["token"])
        self.assertFalse(ok)
        self.assertIn("時計アプリ", message)
