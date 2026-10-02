import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

import research_worker


def make_summary(unresolved_items=None, follow_up_queries=None):
    return {
        "result_text": "結果テキスト",
        "memo_title": "メモタイトル",
        "memo_text": "メモ本文",
        "notification_title": "通知タイトル",
        "completion_text": "完了しました",
        "notification_summary": "要約",
        "notification_detailed": "詳細",
        "unresolved_items": unresolved_items or [],
        "follow_up_queries": follow_up_queries or [],
    }


def make_candidate(url, title="t", description="d", query="q"):
    return {"title": title, "url": url, "description": description, "query": query}


class TestPlanQueries(unittest.TestCase):
    def test_plan_queries_removes_duplicates_and_empty(self):
        with patch.object(research_worker, "ask_llm") as mock_ask:
            mock_ask.return_value = {"queries": ["a", " a ", "b", ""]}
            result = research_worker.plan_queries("obj", [], "base", datetime.now(timezone.utc))
            self.assertEqual(result, ["a", "b"])

    def test_plan_queries_fallback_on_exception(self):
        with patch.object(research_worker, "ask_llm") as mock_ask:
            mock_ask.side_effect = Exception("fail")
            result = research_worker.plan_queries("obj", [], "base", datetime.now(timezone.utc))
            expected = research_worker.build_queries("obj", [], "base")
            self.assertEqual(result, expected)

    def test_plan_queries_fallback_on_missing_queries_key(self):
        with patch.object(research_worker, "ask_llm") as mock_ask:
            mock_ask.return_value = {"other": "data"}
            result = research_worker.plan_queries("obj", [], "base", datetime.now(timezone.utc))
            expected = research_worker.build_queries("obj", [], "base")
            self.assertEqual(result, expected)

    def test_plan_queries_truncates_to_max(self):
        with patch.object(research_worker, "ask_llm") as mock_ask:
            queries = [f"q{i}" for i in range(10)]
            mock_ask.return_value = {"queries": queries}
            result = research_worker.plan_queries("obj", [], "base", datetime.now(timezone.utc))
            self.assertEqual(len(result), research_worker.MAX_RESEARCH_QUERIES)


class TestSelectPages(unittest.TestCase):
    def test_select_pages_no_llm_when_under_limit(self):
        candidates = [make_candidate(f"http://example.com/{i}") for i in range(3)]
        with patch.object(research_worker, "ask_llm") as mock_ask:
            result = research_worker.select_pages("obj", [], candidates, 5)
            mock_ask.assert_not_called()
            self.assertEqual(result, candidates)

    def test_select_pages_reorders_by_llm_choice(self):
        candidates = [make_candidate(f"http://example.com/{i}") for i in range(4)]
        with patch.object(research_worker, "ask_llm") as mock_ask:
            mock_ask.return_value = {"read": [2, 0]}
            result = research_worker.select_pages("obj", [], candidates, 2)
            self.assertEqual([c["url"] for c in result], [
                "http://example.com/2",
                "http://example.com/0",
                "http://example.com/1",
                "http://example.com/3",
            ])

    def test_select_pages_ignores_invalid_indices(self):
        candidates = [make_candidate(f"http://example.com/{i}") for i in range(4)]
        with patch.object(research_worker, "ask_llm") as mock_ask:
            mock_ask.return_value = {"read": [10, 1, 1, True, "2", 0]}
            result = research_worker.select_pages("obj", [], candidates, 2)
            self.assertEqual([c["url"] for c in result], [
                "http://example.com/1",
                "http://example.com/0",
                "http://example.com/2",
                "http://example.com/3",
            ])

    def test_select_pages_fallback_on_exception(self):
        candidates = [make_candidate(f"http://example.com/{i}") for i in range(4)]
        with patch.object(research_worker, "ask_llm") as mock_ask:
            mock_ask.side_effect = Exception("fail")
            result = research_worker.select_pages("obj", [], candidates, 2)
            self.assertEqual(result, candidates)


class TestRunResearch(unittest.TestCase):
    def test_run_research_follow_up_search(self):
        candidates = [make_candidate("http://example.com/1")]
        # Only a follow-up page that was actually read justifies a second summary.
        extra_candidates = [dict(make_candidate("http://example.com/2"), page_text="本文")]
        summary1 = make_summary(
            unresolved_items=["missing"],
            follow_up_queries=["new query"],
        )
        summary2 = make_summary()

        with patch.object(research_worker, "plan_queries") as mock_plan, \
             patch.object(research_worker, "search_web") as mock_search, \
             patch.object(research_worker, "select_pages") as mock_select, \
             patch.object(research_worker, "enrich_candidates") as mock_enrich, \
             patch.object(research_worker, "summarize_sources") as mock_summarize, \
             patch.object(research_worker, "ensure_japanese_research_output") as mock_ensure:

            mock_plan.return_value = ["q1"]
            mock_search.side_effect = [candidates, extra_candidates]
            mock_select.side_effect = [candidates, extra_candidates]
            mock_enrich.side_effect = [candidates, extra_candidates]
            mock_summarize.side_effect = [summary1, summary2]
            mock_ensure.side_effect = lambda x: x

            result, urls = research_worker.run_research("obj", [], "base")

            self.assertEqual(mock_search.call_count, 2)
            self.assertEqual(mock_summarize.call_count, 2)
            self.assertIn("http://example.com/2", urls)

    def test_run_research_no_follow_up_when_empty(self):
        candidates = [make_candidate("http://example.com/1")]
        summary = make_summary(unresolved_items=[], follow_up_queries=[])

        with patch.object(research_worker, "plan_queries") as mock_plan, \
             patch.object(research_worker, "search_web") as mock_search, \
             patch.object(research_worker, "select_pages") as mock_select, \
             patch.object(research_worker, "enrich_candidates") as mock_enrich, \
             patch.object(research_worker, "summarize_sources") as mock_summarize, \
             patch.object(research_worker, "ensure_japanese_research_output") as mock_ensure:

            mock_plan.return_value = ["q1"]
            mock_search.return_value = candidates
            mock_select.return_value = candidates
            mock_enrich.return_value = candidates
            mock_summarize.return_value = summary
            mock_ensure.side_effect = lambda x: x

            result, urls = research_worker.run_research("obj", [], "base")

            self.assertEqual(mock_search.call_count, 1)
            self.assertEqual(mock_summarize.call_count, 1)


if __name__ == "__main__":
    unittest.main()
