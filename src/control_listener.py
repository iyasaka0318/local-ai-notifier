"""Consume one-tap commands published back from the phone.

The undo button in an ntfy notification posts to a control topic. This listener
is the receiving half: it reuses the streaming pattern the Tasks signal listener
already relies on, so one-tap undo needs no new infrastructure.
"""

import json
import sqlite3
import sys
import time

import requests

from action_log import run_pending_calendar_deletes, undo_action
from calendar_worker import delete_calendar_event
from project_paths import DB_PATH
from reminder_worker import send_notification
from report_notifier import control_topic
from state_store import ensure_schema


NTFY_URL = "https://ntfy.sh"
RECONNECT_MAX_SECONDS = 60
UNDO_PREFIX = "undo:"
MAX_TOKENS_PER_COMMAND = 20


def parse_command(message):
    """Return (kind, payload) or (None, None) for anything unrecognized."""
    message = (message or "").strip()
    if not message.startswith(UNDO_PREFIX):
        return None, None
    tokens = [
        token.strip()
        for token in message[len(UNDO_PREFIX):].split(",")
        if token.strip()
    ]
    if not tokens:
        return None, None
    return "undo", tokens[:MAX_TOKENS_PER_COMMAND]


def handle_undo(conn, topic, tokens, sender=send_notification,
                calendar_deleter=delete_calendar_event):
    messages = []
    pending_deletes = []
    for token in tokens:
        try:
            _ok, message = undo_action(conn, token, pending_deletes)
        except Exception as error:
            message = f"取り消しに失敗しました: {error}"
        messages.append(message)
    conn.commit()

    # Remote deletes happen after the local state is durable, so a network
    # failure cannot leave the cancellation half-applied.
    for error in run_pending_calendar_deletes(pending_deletes, calendar_deleter):
        messages.append(f"カレンダーからの削除に失敗しました: {error}")

    sender(topic, "\n".join(messages), "取り消しました")
    return messages


def handle_message(conn, topic, message, sender=send_notification,
                   calendar_deleter=delete_calendar_event):
    kind, payload = parse_command(message)
    if kind != "undo":
        return False
    handle_undo(conn, topic, payload, sender=sender, calendar_deleter=calendar_deleter)
    return True


def listen_forever(topic, connect=None):
    """Block on the control topic, reconnecting with backoff."""
    stream_url = f"{NTFY_URL}/{control_topic(topic)}/json?since=10m"
    retry_seconds = 1
    seen_event_ids = set()
    connect = connect or (lambda: sqlite3.connect(DB_PATH, timeout=30))

    conn = connect()
    ensure_schema(conn)
    try:
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
                        event_id = str(event.get("id") or "")
                        if event_id and event_id in seen_event_ids:
                            continue
                        if event_id:
                            seen_event_ids.add(event_id)
                            if len(seen_event_ids) > 1000:
                                seen_event_ids = {event_id}
                        try:
                            handle_message(conn, topic, event.get("message"))
                        except Exception as error:
                            print(f"取り消し処理に失敗しました: {error}",
                                  file=sys.stderr, flush=True)
            except KeyboardInterrupt:
                raise
            except Exception as error:
                print(f"取り消し受信を再試行します: {error}", file=sys.stderr, flush=True)
                time.sleep(retry_seconds)
                retry_seconds = min(retry_seconds * 2, RECONNECT_MAX_SECONDS)
    finally:
        conn.close()
