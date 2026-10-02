import unittest
import unittest.mock
import monitor_llm


class TestVerifyFoundPage(unittest.TestCase):
    def test_confirmed_true_strips_evidence(self):
        def fake_fetch(url):
            return ("https://example.com/final", "参加登録は10月1日から")

        def fake_ask(prompt, payload, schema):
            return {"confirmed": True, "evidence": " 10月1日から受付 "}

        result = monitor_llm.verify_found_page(
            "参加登録開始を監視",
            {"url": "https://example.com", "title": "T"},
            fetch=fake_fetch,
            ask=fake_ask,
        )
        self.assertEqual(result["confirmed"], True)
        self.assertEqual(result["evidence"], "10月1日から受付")

    def test_ask_receives_final_url_text_and_request(self):
        captured = {}

        def fake_fetch(url):
            return ("https://example.com/final", "本文テキスト")

        def fake_ask(prompt, payload, schema):
            captured["payload"] = payload
            return {"confirmed": False, "evidence": "x"}

        monitor_llm.verify_found_page(
            "依頼文",
            {"url": "https://example.com", "title": "T"},
            fetch=fake_fetch,
            ask=fake_ask,
        )
        self.assertEqual(captured["payload"]["page"]["url"], "https://example.com/final")
        self.assertEqual(captured["payload"]["page"]["text"], "本文テキスト")
        self.assertEqual(captured["payload"]["request"], "依頼文")

    def test_confirmed_false(self):
        def fake_fetch(url):
            return ("https://example.com/final", "準備中")

        def fake_ask(prompt, payload, schema):
            return {"confirmed": False, "evidence": "準備中"}

        result = monitor_llm.verify_found_page(
            "依頼",
            {"url": "https://example.com", "title": "T"},
            fetch=fake_fetch,
            ask=fake_ask,
        )
        self.assertEqual(result["confirmed"], False)

    def test_fetch_exception_returns_none_and_skips_ask(self):
        called = {"ask": False}

        def fake_fetch(url):
            raise RuntimeError("boom")

        def fake_ask(prompt, payload, schema):
            called["ask"] = True
            return {"confirmed": True, "evidence": "x"}

        result = monitor_llm.verify_found_page(
            "依頼",
            {"url": "https://example.com", "title": "T"},
            fetch=fake_fetch,
            ask=fake_ask,
        )
        self.assertIsNone(result["confirmed"])
        self.assertFalse(called["ask"])

    def test_blank_page_returns_none_and_skips_ask(self):
        called = {"ask": False}

        def fake_fetch(url):
            return ("https://example.com/final", "   \n  ")

        def fake_ask(prompt, payload, schema):
            called["ask"] = True
            return {"confirmed": True, "evidence": "x"}

        result = monitor_llm.verify_found_page(
            "依頼",
            {"url": "https://example.com", "title": "T"},
            fetch=fake_fetch,
            ask=fake_ask,
        )
        self.assertIsNone(result["confirmed"])
        self.assertFalse(called["ask"])


class TestGenerateSearchQueries(unittest.TestCase):
    def test_strips_and_dedupes_queries(self):
        with unittest.mock.patch.object(monitor_llm, "ask_llm") as mock_ask:
            mock_ask.return_value = {"search_queries": [" a ", "a", "", "b"]}
            result = monitor_llm.generate_search_queries("依頼")
        self.assertEqual(result, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
