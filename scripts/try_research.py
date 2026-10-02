"""Run one research request end to end without touching the job queue.

Run with: ./runtime/run-worker.sh scripts/try_research.py "目的" [項目 ...]
"""
import sys
import time

sys.path.insert(0, "src")
import research_worker  # noqa: E402

objective, items = sys.argv[1], sys.argv[2:]
calls = []
original_search = research_worker.search_web


def traced_search(queries):
    calls.append(list(queries))
    return original_search(queries)


research_worker.search_web = traced_search
started = time.time()
result, urls = research_worker.run_research(objective, items, objective)
print(f"{time.time() - started:.1f}s  検索 {len(calls)} 回")
for index, queries in enumerate(calls, 1):
    print(f"  検索{index}: {queries}")
print("未解決:", result.get("unresolved_items"))
print("--- 通知（要約） ---")
print(result["notification_summary"])
print("--- 調査結果 ---")
print(result["result_text"][:1500])
