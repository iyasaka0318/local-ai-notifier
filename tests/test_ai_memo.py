import unittest

from ai_memo import is_ready_to_trash, parse_ai_memo, trash_processed_ai_memo


class AIMemoTests(unittest.TestCase):
    def test_full_marker_form_is_strong_and_stripped(self):
        parsed = parse_ai_memo(
            "[AI] リマインダー",
            "AIメモ\n明日の午前8時に洗濯を取り込んでって通知して\nAIメモ",
        )
        self.assertTrue(parsed.explicit)
        self.assertEqual(parsed.confidence, "strong")
        self.assertEqual(parsed.clean_title, "リマインダー")
        self.assertEqual(
            parsed.text_for_ai,
            "明日の午前8時に洗濯を取り込んでって通知して",
        )

    def test_title_only_is_explicit(self):
        parsed = parse_ai_memo(" [AI] 買い物", "牛乳を買う")
        self.assertTrue(parsed.explicit)
        self.assertEqual(parsed.confidence, "explicit")
        self.assertEqual(parsed.text_for_ai, "牛乳を買う")

    def test_single_edge_marker_is_explicit(self):
        self.assertTrue(parse_ai_memo("", "AIメモ、明日知らせて").explicit)
        self.assertTrue(parse_ai_memo("", "明日知らせて、AIメモ").explicit)

    def test_marker_mention_inside_normal_text_is_not_explicit(self):
        parsed = parse_ai_memo("仕様書", "GeminiのAIメモ機能について調べる")
        self.assertFalse(parsed.explicit)
        self.assertEqual(parsed.text_for_ai, "GeminiのAIメモ機能について調べる")

    def test_japanese_commas_around_markers_are_removed(self):
        parsed = parse_ai_memo(
            "[AI] AIメモ",
            "AIメモ、バイト先にお菓子を買う、AIメモ",
        )
        self.assertTrue(parsed.explicit)
        self.assertEqual(parsed.text_for_ai, "バイト先にお菓子を買う")

    def test_transport_instruction_after_closing_marker_is_removed(self):
        parsed = parse_ai_memo(
            "[AI] AIメモ",
            "AIメモ、部屋の掃除をするAIメモタスクスに保存して",
        )
        self.assertTrue(parsed.end_marker)
        self.assertEqual(parsed.text_for_ai, "部屋の掃除をする")

    def test_transport_instruction_without_closing_marker_requires_other_marker(self):
        parsed = parse_ai_memo(
            "[AI] リマインダー",
            "バイト先にお菓子を買う、Google Tasksに追加してください",
        )
        self.assertEqual(parsed.text_for_ai, "バイト先にお菓子を買う")

        normal = parse_ai_memo(
            "通常タスク",
            "Google Tasksに追加してください",
        )
        self.assertFalse(normal.explicit)
        self.assertEqual(normal.text_for_ai, "Google Tasksに追加してください")

    def test_fuzzy_markers_and_fuzzy_transport_noise_are_removed(self):
        parsed = parse_ai_memo(
            "[AI] AIメモ",
            "aiめむバイト先にお菓子を買うAIめまたすくなんとか",
        )
        self.assertTrue(parsed.explicit)
        self.assertEqual(parsed.confidence, "strong")
        self.assertEqual(parsed.text_for_ai, "バイト先にお菓子を買う")

    def test_two_fuzzy_markers_are_accepted_without_title_marker(self):
        parsed = parse_ai_memo(
            "音声入力",
            "エーアイめも部屋の掃除をする、あいメム",
        )
        self.assertTrue(parsed.explicit)
        self.assertEqual(parsed.text_for_ai, "部屋の掃除をする")

    def test_fuzzy_transport_tail_is_removed_only_for_explicit_ai_input(self):
        explicit = parse_ai_memo(
            "[AI] AIメモ",
            "部屋の掃除をする、たすくなんとか",
        )
        self.assertEqual(explicit.text_for_ai, "部屋の掃除をする")

        normal = parse_ai_memo(
            "通常タスク",
            "部屋の掃除をする、たすくなんとか",
        )
        self.assertFalse(normal.explicit)
        self.assertEqual(normal.text_for_ai, "部屋の掃除をする、たすくなんとか")

    def test_trash_calls_note_then_sync(self):
        calls = []

        class Note:
            def trash(self):
                calls.append("trash")

        class Keep:
            def sync(self):
                calls.append("sync")

        trash_processed_ai_memo(Keep(), Note())
        self.assertEqual(calls, ["trash", "sync"])

    def test_incomplete_actions_are_not_ready_to_trash(self):
        self.assertFalse(
            is_ready_to_trash({
                "intent": "reminder",
                "scheduled_at": None,
                "needs_confirmation": False,
            })
        )
        self.assertFalse(
            is_ready_to_trash({
                "intent": "unknown",
                "needs_confirmation": False,
            })
        )
        self.assertFalse(
            is_ready_to_trash({
                "intent": "reminder",
                "scheduled_at": "tomorrow morning",
                "needs_confirmation": False,
            })
        )
        self.assertTrue(
            is_ready_to_trash({
                "intent": "reminder",
                "scheduled_at": "2026-09-18T08:00:00+09:00",
                "needs_confirmation": False,
            })
        )


if __name__ == "__main__":
    unittest.main()
