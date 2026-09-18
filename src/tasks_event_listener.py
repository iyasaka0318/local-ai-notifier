import json
import os
import sys
import threading
import time

import requests

from control_listener import listen_forever as listen_for_commands
from inbox_cycle import run_inbox_cycle
from instance_lock import SingleInstanceLock
from project_paths import CONFIG_DIR, RUNTIME_DIR, ensure_runtime_directories


CONFIG_FILE = os.environ.get(
    "AI_TASKS_EVENT_CONFIG_FILE",
    str(CONFIG_DIR / "tasks_event.json"),
)
RECONNECT_MAX_SECONDS = 60
FALLBACK_CHECK_SECONDS = 6 * 60 * 60


def load_signal_url():
    with open(CONFIG_FILE, encoding="utf-8") as file:
        value = json.load(file).get("signal_url", "").strip().rstrip("/")
    if not value.startswith("https://"):
        raise RuntimeError("tasks_event.jsonのsignal_urlが不正です")
    return value


def process_cycle(reason):
    print(f"AI Inbox確認を開始します: {reason}", flush=True)
    result = run_inbox_cycle()
    print(f"AI Inbox確認が終了しました: exit={result}", flush=True)


def listen_forever(signal_url):
    stream_url = signal_url + "/json?since=10m"
    retry_seconds = 1
    last_cycle = time.monotonic()
    seen_event_ids = set()

    # PC停止中に追加されたタスクも拾えるよう、起動直後に必ず確認します。
    process_cycle("起動時")

    while True:
        try:
            with requests.get(stream_url, stream=True, timeout=(15, 90)) as response:
                response.raise_for_status()
                retry_seconds = 1
                for raw_line in response.iter_lines(decode_unicode=True):
                    now = time.monotonic()
                    if now - last_cycle >= FALLBACK_CHECK_SECONDS:
                        process_cycle("6時間ごとの保険確認")
                        last_cycle = time.monotonic()
                    if not raw_line:
                        continue
                    event = json.loads(raw_line)
                    if event.get("event") != "message":
                        continue
                    if event.get("message") != "tasks_changed":
                        continue
                    event_id = str(event.get("id") or "")
                    if event_id and event_id in seen_event_ids:
                        continue
                    if event_id:
                        seen_event_ids.add(event_id)
                        # 接続が長期間続いてもメモリ使用量を一定に保ちます。
                        if len(seen_event_ids) > 1000:
                            seen_event_ids = {event_id}
                    process_cycle("Google Tasks更新信号")
                    last_cycle = time.monotonic()
        except KeyboardInterrupt:
            raise
        except Exception as error:
            print(f"起動信号接続を再試行します: {error}", file=sys.stderr, flush=True)
            time.sleep(retry_seconds)
            retry_seconds = min(retry_seconds * 2, RECONNECT_MAX_SECONDS)


def start_command_listener(topic):
    """Watch the control topic in the background so undo works while idle."""
    if not topic:
        print("NTFY_TOPICが未設定のため、ワンタップ取り消しは無効です。", file=sys.stderr)
        return None
    thread = threading.Thread(
        target=listen_for_commands,
        args=(topic,),
        name="ntfy-control-listener",
        daemon=True,
    )
    thread.start()
    return thread


def main():
    ensure_runtime_directories()
    lock = SingleInstanceLock(str(RUNTIME_DIR / "tasks_event_listener.lock"))
    if not lock.acquire():
        print("Google Tasks起動信号リスナーは既に動作中です。")
        return 0
    start_command_listener(os.environ.get("NTFY_TOPIC", "").strip())
    listen_forever(load_signal_url())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
