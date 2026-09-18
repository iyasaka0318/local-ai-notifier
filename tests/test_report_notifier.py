import unittest

from report_notifier import (
    build_undo_action,
    control_topic,
    format_report,
    send_execution_report,
)


class ReportNotifierTests(unittest.TestCase):
    def test_control_topic_defaults_to_a_suffix(self):
        self.assertEqual(control_topic("my-topic"), "my-topic-control")

    def test_single_item_title_names_the_kind(self):
        title, message = format_report([
            {"kind": "calendar", "summary": "歯医者", "detail": "2026年10月03日 14:00"},
        ])
        self.assertEqual(title, "カレンダー予定を登録しました")
        self.assertIn("歯医者", message)
        self.assertIn("2026年10月03日 14:00", message)

    def test_multiple_items_are_counted(self):
        title, message = format_report([
            {"kind": "calendar", "summary": "歯医者", "detail": "10月3日"},
            {"kind": "persistent_reminder", "summary": "牛乳を買う"},
            {"kind": "web_monitor", "summary": "第74回の登録開始"},
        ])
        self.assertEqual(title, "3件を登録しました")
        for expected in ("歯医者", "牛乳を買う", "第74回の登録開始"):
            self.assertIn(expected, message)

    def test_fallback_reason_is_shown_to_the_user(self):
        _title, message = format_report([
            {
                "kind": "persistent_reminder",
                "summary": "洗濯をする",
                "fallback_reason": "時刻が読み取れなかったので、継続リマインドとして保存しました。",
            },
        ])
        self.assertIn("※ 時刻が読み取れなかったので", message)

    def test_undo_action_targets_the_control_topic(self):
        action = build_undo_action("my-topic", ["abc", "def"])
        self.assertEqual(action["action"], "http")
        self.assertEqual(action["method"], "POST")
        self.assertEqual(action["url"], "https://ntfy.sh/my-topic-control")
        self.assertEqual(action["body"], "undo:abc,def")
        self.assertTrue(action["clear"])

    def test_undo_action_is_omitted_without_tokens(self):
        self.assertIsNone(build_undo_action("my-topic", []))

    def test_report_attaches_one_undo_button_for_the_whole_note(self):
        sent = []
        send_execution_report(
            "my-topic",
            [
                {"kind": "calendar", "summary": "歯医者", "token": "t1"},
                {"kind": "todo", "summary": "牛乳", "token": "t2"},
            ],
            sender=sent.append,
        )
        payload = sent[0]
        self.assertEqual(len(payload["actions"]), 1)
        self.assertEqual(payload["actions"][0]["label"], "すべて取り消し")
        self.assertEqual(payload["actions"][0]["body"], "undo:t1,t2")

    def test_single_item_uses_the_singular_label(self):
        sent = []
        send_execution_report(
            "my-topic",
            [{"kind": "todo", "summary": "牛乳", "token": "t1"}],
            sender=sent.append,
        )
        self.assertEqual(sent[0]["actions"][0]["label"], "取り消し")

    def test_nothing_is_sent_without_entries(self):
        sent = []
        self.assertFalse(send_execution_report("my-topic", [], sender=sent.append))
        self.assertEqual(sent, [])

    def test_report_priority_is_below_a_real_alert(self):
        sent = []
        send_execution_report(
            "my-topic", [{"kind": "memo", "summary": "x", "token": "t"}],
            sender=sent.append,
        )
        self.assertEqual(sent[0]["priority"], 3)


if __name__ == "__main__":
    unittest.main()
