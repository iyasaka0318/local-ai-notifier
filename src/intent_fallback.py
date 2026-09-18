"""Force every classified item into an executable intent.

The AI Inbox is voice-driven, so asking the user a follow-up question is never
an acceptable outcome: a held-back item is an item the user has to rediscover
and fix by hand. Every ambiguous classification is therefore routed to the
closest intent that can still run unattended, and the substitution is reported
back so the user can correct it with one more utterance.
"""

from datetime import date, datetime


# Reasons are user-facing and travel straight into the ntfy report.
REMINDER_WITHOUT_TIME = "時刻が読み取れなかったので、継続リマインドとして保存しました。"
CALENDAR_WITHOUT_DATE = "日付が読み取れなかったので、TODOとして保存しました。"
UNKNOWN_AS_MEMO = "内容を判断しきれなかったので、メモとして保存しました。"
PERSISTENT_WITHOUT_TEXT = "リマインドする内容が読み取れなかったので、メモとして保存しました。"
RESEARCH_WITHOUT_OBJECTIVE = "調べる対象が読み取れなかったので、TODOとして保存しました。"
MONITOR_WITHOUT_TARGET = "監視対象が読み取れなかったので、TODOとして保存しました。"


UNPARSABLE_TIME = "日時を解釈できなかったので、継続リマインドとして保存しました。"
UNPARSABLE_DATE = "日付を解釈できなかったので、TODOとして保存しました。"
CALENDAR_NOT_READY = "予定として確定できなかったので、TODOとして保存しました。"


def _blank(value):
    return not (value or "").strip()


def _parsable_datetime(value):
    """True only when the worker will actually be able to use this value.

    Checking for a non-empty string is not enough: the classifier can return
    "あした" or a malformed offset, which survives an emptiness check and then
    strands the item in a status no worker selects.
    """
    if _blank(value):
        return False
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _parsable_date(value):
    if _blank(value):
        return False
    try:
        date.fromisoformat(value.strip()[:10])
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def apply_intent_fallback(item):
    """Return (item, reason). ``reason`` is None when nothing was rewritten."""
    item = dict(item)
    intent = item.get("intent")

    if intent == "reminder" and not _parsable_datetime(item.get("scheduled_at")):
        # Decide the reason before the field is cleared below, so the report
        # distinguishes "no time was said" from "the time could not be read".
        reason = (
            REMINDER_WITHOUT_TIME if _blank(item.get("scheduled_at"))
            else UNPARSABLE_TIME
        )
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
        return item, reason

    if intent == "calendar":
        # calendar_worker only selects rows with calendar_ready = 1 and a usable
        # start, so anything short of that would sit in waiting_information
        # forever while its source note is already marked processed.
        usable_start = (
            _parsable_date(item.get("event_start"))
            if item.get("all_day")
            else _parsable_datetime(item.get("event_start"))
        )
        if _blank(item.get("event_title")) or not usable_start:
            item["intent"] = "todo"
            item["calendar_ready"] = False
            return item, (
                CALENDAR_WITHOUT_DATE if _blank(item.get("event_start"))
                else UNPARSABLE_DATE
            )
        if not item.get("calendar_ready"):
            item["intent"] = "todo"
            return item, CALENDAR_NOT_READY

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
