import unittest

from classification_output import rewrite_classification_output


class ClassificationOutputTests(unittest.TestCase):
    def test_multiple_english_fields_are_rewritten_in_one_call(self):
        result = {
            "intent": "calendar",
            "summary": "Schedule a project meeting",
            "notification_text": "Meeting was added",
            "event_title": "Project meeting",
            "event_location": None,
            "event_description": None,
            "persistent_task_text": None,
            "missing_information": ["Meeting room"],
            "actions": [{
                "type": "notify",
                "objective": "Notify after creation",
                "requested_items": ["Event time"],
            }],
        }
        calls = []

        def rewrite(value):
            calls.append(value)
            return {
                **value,
                "intent": "memo",
                "summary": "プロジェクト会議を予定する",
                "notification_text": "会議を追加しました",
                "event_title": "プロジェクト会議",
                "missing_information": ["会議室"],
                "actions": [{
                    "type": "research",
                    "objective": "作成後に通知する",
                    "requested_items": ["開催時刻"],
                }],
            }

        rewritten = rewrite_classification_output(result, rewrite)

        self.assertEqual(len(calls), 1)
        self.assertEqual(rewritten["intent"], "calendar")
        self.assertEqual(rewritten["actions"][0]["type"], "notify")
        self.assertEqual(rewritten["summary"], "プロジェクト会議を予定する")
        self.assertEqual(rewritten["missing_information"], ["会議室"])

    def test_japanese_output_does_not_call_rewriter(self):
        result = {"summary": "会議を予定する", "actions": []}
        calls = []
        self.assertIs(
            rewrite_classification_output(result, lambda value: calls.append(value)),
            result,
        )
        self.assertEqual(calls, [])

    def test_rewriter_cannot_change_missing_item_counts(self):
        result = {
            "summary": "Needs more information",
            "missing_information": ["日時"],
            "actions": [{
                "type": "notify",
                "objective": "Notify",
                "requested_items": ["場所"],
            }],
        }

        rewritten = rewrite_classification_output(result, lambda value: {
            **value,
            "summary": "追加情報が必要です",
            "missing_information": [],
            "actions": [{
                "type": "research",
                "objective": "通知する",
                "requested_items": ["場所", "参加者"],
            }],
        })

        self.assertEqual(rewritten["missing_information"], ["日時"])
        self.assertEqual(rewritten["actions"][0]["requested_items"], ["場所"])


if __name__ == "__main__":
    unittest.main()
