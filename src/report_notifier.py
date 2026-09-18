"""Tell the user what actually happened, with a one-tap way to undo it.

The input channel is voice, so the system never asks a follow-up question. That
only works if every accepted utterance produces a short, unambiguous report: the
report is where the user notices a misheard word, and the undo button is how
they fix it without opening anything.
"""

import os

import requests

from action_log import KIND_LABELS, UNDOABLE_KINDS


NTFY_URL = "https://ntfy.sh"
MAX_NTFY_ACTIONS = 3


def control_topic(topic):
    """Topic the phone publishes undo commands back to."""
    configured = os.environ.get("NTFY_CONTROL_TOPIC", "").strip()
    if configured:
        return configured
    return f"{topic}-control"


def build_undo_action(topic, tokens, label="取り消し"):
    tokens = [token for token in tokens if token]
    if not tokens:
        return None
    return {
        "action": "http",
        "label": label,
        "url": f"{NTFY_URL}/{control_topic(topic)}",
        "method": "POST",
        "body": "undo:" + ",".join(tokens),
        "clear": True,
    }


def format_report(entries):
    """Return (title, message) describing everything one note produced."""
    blocks = []
    for entry in entries:
        label = KIND_LABELS.get(entry["kind"], entry["kind"])
        header = label
        if entry.get("detail"):
            header = f"{label}  {entry['detail']}"
        block = f"{header}\n{entry['summary']}"
        if entry.get("fallback_reason"):
            block += f"\n※ {entry['fallback_reason']}"
        blocks.append(block)

    if len(entries) == 1:
        label = KIND_LABELS.get(entries[0]["kind"], entries[0]["kind"])
        title = f"{label}を登録しました"
    else:
        title = f"{len(entries)}件を登録しました"
    return title, "\n\n".join(blocks)


def send_execution_report(topic, entries, sender=None):
    """Send one report per note. Returns False when there is nothing to report."""
    entries = [entry for entry in entries if entry.get("summary")]
    if not topic or not entries:
        return False

    title, message = format_report(entries)
    undo_tokens = [
        entry["token"] for entry in entries
        if entry.get("token") and entry["kind"] in UNDOABLE_KINDS
    ]
    label = "取り消し" if len(undo_tokens) == 1 else "すべて取り消し"
    action = build_undo_action(topic, undo_tokens, label)

    payload = {
        "topic": topic,
        "title": title[:120],
        "message": message[:3500],
        # Lower than an actual reminder: this is a receipt, not an alert.
        "priority": 3,
    }
    if action:
        payload["actions"] = [action][:MAX_NTFY_ACTIONS]

    (sender or _default_sender)(payload)
    return True


def _default_sender(payload):
    response = requests.post(NTFY_URL, json=payload, timeout=30)
    response.raise_for_status()
