"""Time classify_note end to end and show the fields that drive routing.

Run with: ./runtime/run-worker.sh scripts/bench_classify.py [text ...]
"""
import contextlib
import io
import sys
import time

sys.path.insert(0, "src")
import watch_keep  # noqa: E402

DEFAULT_TEXTS = [
    "テスト",
    "明日の10時に歯医者って通知して",
    "牛乳買うのと、来週の火曜15時に面談を予定に入れて",
    "群馬の今週末の天気を調べて",
    "10月10日に研究室の飲み会、18時から駅前で",
    "毎週月曜の朝にゴミ出しをリマインドして",
    "卵買うのを忘れないように覚えといて",
    "駐車場は3階のBの12番",
    "東京ゲームショーのチケット販売ページを見張っといて",
    "起きた",
]
SHOWN = (
    "scheduled_at", "recurrence", "event_start", "calendar_ready",
    "persistent_reminder_action", "persistent_task_text",
)

for text in sys.argv[1:] or DEFAULT_TEXTS:
    started = time.time()
    with contextlib.redirect_stdout(io.StringIO()):
        items = watch_keep.normalize_items(watch_keep.classify_note(text))
    elapsed = time.time() - started
    print(f"{elapsed:5.1f}s {text}")
    for item in items:
        shown = {key: item[key] for key in SHOWN if item.get(key)}
        print(f"        {item['intent']}: {item['summary']} {shown}")
