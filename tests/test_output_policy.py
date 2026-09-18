import unittest

from output_policy import infer_research_notification_mode, needs_japanese_rewrite


class OutputPolicyTests(unittest.TestCase):
    def test_notification_nuances(self):
        self.assertEqual(
            infer_research_notification_mode("調べ終わったら通知して"),
            "completion_only",
        )
        self.assertEqual(
            infer_research_notification_mode("結果を通知して"),
            "result_summary",
        )
        self.assertEqual(
            infer_research_notification_mode("調べた内容を詳しく通知して"),
            "detailed_result",
        )
        self.assertEqual(
            infer_research_notification_mode("参加費を調べて通知して"),
            "result_summary",
        )

    def test_english_prose_detection_ignores_urls(self):
        self.assertTrue(needs_japanese_rewrite("Research completed successfully"))
        self.assertFalse(needs_japanese_rewrite("調査が完了しました。"))
        self.assertFalse(needs_japanese_rewrite("https://example.com"))
        self.assertTrue(needs_japanese_rewrite(
            "■ 参考\nThis is a long English paragraph copied from a source page and it should be rewritten into Japanese prose."
        ))
        self.assertTrue(needs_japanese_rewrite(
            "■ 結果\nPrice details\nTicket purchase method\nVenue access information\n"
            "Official visitor registration process\nGeneral admission requirements"
        ))


if __name__ == "__main__":
    unittest.main()
