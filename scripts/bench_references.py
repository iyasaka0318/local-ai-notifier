"""Check that "さっきの〜" resolves against a realistic recent-actions list.

Run with: ./runtime/run-worker.sh scripts/bench_references.py
"""
import contextlib
import io
import sys
import time

sys.path.insert(0, "src")
import watch_keep  # noqa: E402

# Newest first, as recent_actions returns them: a watch, then a research job,
# then a memo and a reminder spoken after it.
RECENT = [
    {"token": "t-rem", "item_id": "n5", "kind": "reminder", "summary": "洗濯物を取り込む",
     "detail": "10/03 18:00", "created_at": "2026-10-02T12:10:00+00:00", "undone": False,
     "request": "明日の18時に洗濯物取り込むって通知して"},
    {"token": "t-memo", "item_id": "n4", "kind": "memo", "summary": "駐車場は3階のBの12番",
     "detail": None, "created_at": "2026-10-02T12:05:00+00:00", "undone": False,
     "request": "駐車場は3階のBの12番"},
    {"token": "t-res", "item_id": "n3", "kind": "research", "summary": "東京ゲームショーの開催日と場所を調べる",
     "detail": None, "created_at": "2026-10-02T12:00:00+00:00", "undone": False,
     "request": "東京ゲームショーっていつどこでやるのか調べて",
     "result": "東京ゲームショー2026は9月17日〜20日に幕張メッセで開催。"},
    {"token": "t-mon", "item_id": "n2", "kind": "web_monitor",
     "summary": "emb 研究発表会第73回の参加登録が始まったら通知する",
     "detail": None, "created_at": "2026-10-02T11:53:00+00:00", "undone": False,
     "request": "emb 研究発表会第73回の発表 じゃない方 参加の方の登録が始まったら俺に通知"},
]
CASES = [
    "さっきの監視登録のやつ、今とりあえずあるか調べて",
    "さっき言ったEMB研究会のやつ、今もう始まってるか調べて",
    "直近の監視のやつ、参加じゃなくて発表申込の方にして",
    "この前のメモに、ゲートは北口って足しといて",
    "さっきの調べもの、チケットの料金も調べて",
    "さっきのリマインダーやっぱり19時にして",
    "最後の監視やっぱりなしで",
    "牛乳買うの覚えといて",
]
SHOWN = (
    "reference_target", "correction_target", "correction_action",
    "correction_text", "scheduled_at",
)

for text in sys.argv[1:] or CASES:
    started = time.time()
    with contextlib.redirect_stdout(io.StringIO()):
        items = watch_keep.normalize_items(watch_keep.classify_note(text, recent=RECENT))
    print(f"{time.time() - started:5.1f}s {text}")
    for item in items:
        shown = {key: item[key] for key in SHOWN if item.get(key)}
        print(f"        {item['intent']}: {item['summary']} {shown}")
        for action in item.get("actions") or []:
            if action.get("type") == "research":
                print(f"          objective: {action.get('objective')} / {action.get('requested_items')}")
