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
# This is the recovery path for input that arrives without a signal, which is
# everything entered through Gemini. It used to be a distant backstop behind
# Apps Script's own poll; now that the poll's only job was to reach ntfy from
# Google - the one leg that hangs for tens of seconds - this timer is the
# fallback, so it runs often enough to be one.
FALLBACK_CHECK_SECONDS = int(
    os.environ.get("AI_FALLBACK_CHECK_SECONDS", str(15 * 60))
)


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


_cycle_lock = threading.Lock()
_last_cycle = time.monotonic()


def process_cycle_once(reason):
    """Serialize cycles so the stream and the safety timer cannot overlap."""
    global _last_cycle
    with _cycle_lock:
        process_cycle(reason)
        _last_cycle = time.monotonic()


def run_safety_checks(interval=FALLBACK_CHECK_SECONDS, sleeper=time.sleep,
                      clock=time.monotonic, stop=None):
    """Poll Google Tasks on a timer that does not depend on ntfy.

    The previous check only ran when a line arrived from the ntfy stream, so a
    connection outage silenced the very fallback that was supposed to cover it.
    Apps Script's own backup poll signals through the same ntfy topic, so it is
    not an independent recovery path either.
    """
    while stop is None or not stop.is_set():
        sleeper(min(interval, 300))
        if stop is not None and stop.is_set():
            return
        if clock() - _last_cycle >= interval:
            try:
                process_cycle_once("定期確認（ntfy非依存）")
            except Exception as error:
                print(f"定期確認に失敗しました: {error}", file=sys.stderr, flush=True)


def start_safety_timer():
    thread = threading.Thread(
        target=run_safety_checks, name="tasks-safety-timer", daemon=True
    )
    thread.start()
    return thread


def listen_forever(signal_url):
    stream_url = signal_url + "/json?since=10m"
    retry_seconds = 1
    seen_event_ids = set()

    # PC停止中に追加されたタスクも拾えるよう、起動直後に必ず確認します。
    process_cycle_once("起動時")
    start_safety_timer()

    while True:
        try:
            with requests.get(stream_url, stream=True, timeout=(15, 90)) as response:
                response.raise_for_status()
                retry_seconds = 1
                for raw_line in response.iter_lines(decode_unicode=True):
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
                    process_cycle_once("Google Tasks更新信号")
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
