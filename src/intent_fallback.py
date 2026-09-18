"""Force every classified item into an executable intent.

The AI Inbox is voice-driven, so asking the user a follow-up question is never
an acceptable outcome: a held-back item is an item the user has to rediscover
and fix by hand. Every ambiguous classification is therefore routed to the
closest intent that can still run unattended, and the substitution is reported
back so the user can correct it with one more utterance.
"""


# Reasons are user-facing and travel straight into the ntfy report.
REMINDER_WITHOUT_TIME = "時刻が読み取れなかったので、継続リマインドとして保存しました。"
CALENDAR_WITHOUT_DATE = "日付が読み取れなかったので、TODOとして保存しました。"
UNKNOWN_AS_MEMO = "内容を判断しきれなかったので、メモとして保存しました。"
PERSISTENT_WITHOUT_TEXT = "リマインドする内容が読み取れなかったので、メモとして保存しました。"
RESEARCH_WITHOUT_OBJECTIVE = "調べる対象が読み取れなかったので、TODOとして保存しました。"
MONITOR_WITHOUT_TARGET = "監視対象が読み取れなかったので、TODOとして保存しました。"


def _blank(value):
    return not (value or "").strip()


def apply_intent_fallback(item):
    """Return (item, reason). ``reason`` is None when nothing was rewritten."""
    item = dict(item)
    intent = item.get("intent")

    if intent == "reminder" and _blank(item.get("scheduled_at")):
        # A reminder with no resolvable time still has a task in it. Persistent
        # reminders are the one channel that carries a task with no due time.
        task_text = (
            item.get("persistent_task_text")
            or item.get("notification_text")
            or item.get("summary")
        )
        if _blank(task_text):
            return _to_memo(item), UNKNOWN_AS_MEMO
        item["intent"] = "persistent_reminder"
        item["persistent_reminder_action"] = "add"
        item["persistent_task_text"] = task_text
        item["scheduled_at"] = None
        item["recurrence"] = None
        return item, REMINDER_WITHOUT_TIME

    if intent == "calendar" and (
        _blank(item.get("event_start")) or _blank(item.get("event_title"))
    ):
        item["intent"] = "todo"
        item["calendar_ready"] = False
        return item, CALENDAR_WITHOUT_DATE

    if intent == "persistent_reminder":
        action = item.get("persistent_reminder_action")
        if action == "add" and _blank(item.get("persistent_task_text")):
            if _blank(item.get("summary")):
                return _to_memo(item), UNKNOWN_AS_MEMO
            item["persistent_task_text"] = item["summary"]
            return item, PERSISTENT_WITHOUT_TEXT
        if action not in ("add", "complete", "notify_now"):
            # An unusable command is still a task the user voiced.
            item["intent"] = "todo"
            return item, UNKNOWN_AS_MEMO if _blank(item.get("summary")) else CALENDAR_WITHOUT_DATE

    if intent == "research" and _blank(item.get("summary")):
        item["intent"] = "todo"
        return item, RESEARCH_WITHOUT_OBJECTIVE

    if intent == "web_monitor" and _blank(item.get("summary")):
        item["intent"] = "todo"
        return item, MONITOR_WITHOUT_TARGET

    if intent in (None, "", "unknown"):
        return _to_memo(item), UNKNOWN_AS_MEMO

    return item, None


def _to_memo(item):
    item = dict(item)
    item["intent"] = "memo"
    item["calendar_ready"] = False
    item["scheduled_at"] = None
    item["recurrence"] = None
    if _blank(item.get("summary")):
        item["summary"] = (item.get("original_text") or "").strip()[:200] or "内容不明のメモ"
    return item
