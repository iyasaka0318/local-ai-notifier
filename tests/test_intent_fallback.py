import unittest

from intent_fallback import (
    CALENDAR_WITHOUT_DATE,
    REMINDER_WITHOUT_TIME,
    UNKNOWN_AS_MEMO,
    apply_intent_fallback,
)


class IntentFallbackTests(unittest.TestCase):
    """Voice input never gets a follow-up question, so nothing may stall."""

    def test_reminder_without_time_becomes_persistent(self):
        item, reason = apply_intent_fallback({
            "intent": "reminder",
            "summary": "洗濯をする",
            "notification_text": "洗濯をする",
            "scheduled_at": None,
        })
        self.assertEqual(item["intent"], "persistent_reminder")
        self.assertEqual(item["persistent_reminder_action"], "add")
        self.assertEqual(item["persistent_task_text"], "洗濯をする")
        self.assertEqual(reason, REMINDER_WITHOUT_TIME)

    def test_reminder_with_time_is_untouched(self):
        item, reason = apply_intent_fallback({
            "intent": "reminder",
            "summary": "歯医者",
            "scheduled_at": "2026-10-03T14:00:00+09:00",
        })
        self.assertEqual(item["intent"], "reminder")
        self.assertIsNone(reason)

    def test_calendar_without_start_becomes_todo(self):
        item, reason = apply_intent_fallback({
            "intent": "calendar",
            "summary": "歯医者に行く",
            "event_title": "歯医者",
            "event_start": None,
        })
        self.assertEqual(item["intent"], "todo")
        self.assertFalse(item["calendar_ready"])
        self.assertEqual(reason, CALENDAR_WITHOUT_DATE)

    def test_calendar_without_title_becomes_todo(self):
        item, _reason = apply_intent_fallback({
            "intent": "calendar",
            "summary": "何かの予定",
            "event_title": "",
            "event_start": "2026-10-03",
        })
        self.assertEqual(item["intent"], "todo")

    def test_unknown_becomes_memo(self):
        item, reason = apply_intent_fallback({
            "intent": "unknown",
            "summary": "よく分からない話",
        })
        self.assertEqual(item["intent"], "memo")
        self.assertEqual(reason, UNKNOWN_AS_MEMO)

    def test_empty_unknown_falls_back_to_original_text(self):
        item, _reason = apply_intent_fallback({
            "intent": "unknown",
            "summary": "",
            "original_text": "えーっと、なんだっけ",
        })
        self.assertEqual(item["intent"], "memo")
        self.assertEqual(item["summary"], "えーっと、なんだっけ")

    def test_persistent_add_without_text_uses_summary(self):
        item, reason = apply_intent_fallback({
            "intent": "persistent_reminder",
            "persistent_reminder_action": "add",
            "persistent_task_text": "   ",
            "summary": "お菓子を買う",
        })
        self.assertEqual(item["persistent_task_text"], "お菓子を買う")
        self.assertIsNotNone(reason)

    def test_unusable_persistent_command_becomes_todo(self):
        item, _reason = apply_intent_fallback({
            "intent": "persistent_reminder",
            "persistent_reminder_action": None,
            "summary": "何かのタスク",
        })
        self.assertEqual(item["intent"], "todo")

    def test_input_is_not_mutated(self):
        original = {"intent": "unknown", "summary": "x"}
        apply_intent_fallback(original)
        self.assertEqual(original["intent"], "unknown")


if __name__ == "__main__":
    unittest.main()


class ExecutabilityTests(unittest.TestCase):
    """A non-empty field is not the same as a usable one.

    calendar_worker only selects rows with calendar_ready = 1 and a parseable
    start, and dispatch_due_reminders only selects 'pending'. Anything the
    fallback lets through in a weaker state is stranded, while its source note
    is already marked processed.
    """

    def test_calendar_not_ready_becomes_todo(self):
        item, reason = apply_intent_fallback({
            "intent": "calendar", "summary": "打合せ",
            "event_title": "打合せ", "event_start": "2026-10-03T09:00:00+09:00",
            "calendar_ready": False,
        })
        self.assertEqual(item["intent"], "todo")
        self.assertIsNotNone(reason)

    def test_an_unparseable_start_becomes_todo(self):
        item, reason = apply_intent_fallback({
            "intent": "calendar", "summary": "打合せ",
            "event_title": "打合せ", "event_start": "来週", "calendar_ready": True,
        })
        self.assertEqual(item["intent"], "todo")
        self.assertIn("解釈", reason)

    def test_an_unparseable_reminder_time_becomes_persistent(self):
        item, reason = apply_intent_fallback({
            "intent": "reminder", "summary": "掃除",
            "notification_text": "掃除", "scheduled_at": "あした",
        })
        self.assertEqual(item["intent"], "persistent_reminder")
        self.assertIn("解釈", reason)

    def test_an_all_day_date_only_start_is_accepted(self):
        item, reason = apply_intent_fallback({
            "intent": "calendar", "summary": "旅行", "event_title": "旅行",
            "event_start": "2026-10-03", "all_day": True, "calendar_ready": True,
        })
        self.assertEqual(item["intent"], "calendar")
        self.assertIsNone(reason)

    def test_a_usable_calendar_item_is_untouched(self):
        item, reason = apply_intent_fallback({
            "intent": "calendar", "summary": "打合せ", "event_title": "打合せ",
            "event_start": "2026-10-03T09:00:00+09:00", "calendar_ready": True,
        })
        self.assertEqual(item["intent"], "calendar")
        self.assertIsNone(reason)

    def test_the_reason_distinguishes_missing_from_unreadable(self):
        _item, missing = apply_intent_fallback({
            "intent": "reminder", "summary": "掃除", "notification_text": "掃除",
        })
        _item2, unreadable = apply_intent_fallback({
            "intent": "reminder", "summary": "掃除",
            "notification_text": "掃除", "scheduled_at": "あした",
        })
        self.assertNotEqual(missing, unreadable)
