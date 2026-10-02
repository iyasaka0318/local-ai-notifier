"""Time classify_note end to end through the notifier's own client."""
import contextlib
import io
import sys
import time

sys.path.insert(0, "src")
import watch_keep  # noqa: E402

for text in sys.argv[1:] or [
    "テスト", "明日の10時に歯医者って通知して",
    "牛乳買うのと、来週の火曜15時に面談を予定に入れて",
]:
    started = time.time()
    with contextlib.redirect_stdout(io.StringIO()):
        items = watch_keep.normalize_items(watch_keep.classify_note(text))
    print(f"{time.time() - started:5.1f}s {text} -> {[i.get('intent') for i in items]}")
