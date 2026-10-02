"""Answer a spoken question from what the user has already recorded.

The answer is grounded in the local records and the calendar only. When they
do not contain the answer the reply says so: a guess here would be read as a
fact the user once recorded.
"""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from calendar_worker import load_webhook_credentials
from llm_client import ask_llm


LOCAL_TIMEZONE = ZoneInfo("Asia/Tokyo")
# Japanese runs about 0.72 tokens per character, so this stays well inside the
# 32K context with the prompt and the answer. Oldest records are dropped first.
MAX_RECORD_CHARS = 22_000
MAX_RECORD_TEXT_CHARS = 1_200
CALENDAR_PAST_DAYS = 90
CALENDAR_FUTURE_DAYS = 180
# Intents that are commands to this system rather than something recorded.
NOT_RECORDS = (
    "question", "unknown", "wake_briefing", "correction",
    "web_monitor_manage", "reminder_manage",
)
KIND_LABELS = {
    "memo": "メモ",
    "todo": "TODO",
    "reminder": "リマインダー",
    "calendar": "予定",
    "persistent_reminder": "継続リマインド",
    "research": "調べもの",
    "web_monitor": "Web監視",
    "calendar_event": "カレンダー",
    "alarm": "アラーム",
}
STATUS_TABLES = {
    "memo": "memos",
    "todo": "todos",
    "reminder": "reminders",
    "calendar": "calendar_jobs",
}

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "answer": {"type": "string"},
        "source_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["found", "answer", "source_ids"],
}

ANSWER_PROMPT = """
You answer a Japanese user's question about things they previously recorded:
memos, TODOs, reminders, calendar events, saved tasks, research results and web
watches. The question was spoken into a phone, so tolerate speech-recognition
noise and casual wording such as "〜っけ".

Use only the supplied records. Each record has an id, a kind, the time it was
recorded, its text and, where relevant, a status or a scheduled time.

- If the records contain the answer, set found=true, answer briefly in natural
  Japanese, and list in source_ids the ids of the records the answer rests on.
- If they do not, set found=false and say plainly that it is not in the records
  (記録には見当たりません). Never guess, never answer from general knowledge,
  and never invent a date, time, place, number or name. If a record is related
  but does not actually answer the question, you may mention it as related, and
  still set found=false.
- Resolve words such as 先月, 昨日, 来週 against current_datetime. State dates
  as actual dates. recorded_at is when the user said it, which is not always when
  the thing happens: prefer scheduled_at or start for when something takes place.
- When several records fit, prefer the most recent, and say so if they disagree.
- summary reflects later corrections by the user. When text and summary disagree,
  summary is the current version.
- A cancelled or completed record is still a fact about the past; mention its
  status when it matters to the answer.
- If calendar_available is false, the calendar could not be read: for a question
  about a schedule, answer from the other records and say that the calendar
  could not be checked.

The answer is sent as a phone notification: a few short sentences, no markdown.
"""


def _local(value):
    """Render an ISO timestamp in local time; return the input when it is not one."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%d %H:%M")
    return parsed.astimezone(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M")


def _statuses(conn, table):
    return dict(conn.execute(f"SELECT note_id, status FROM {table}").fetchall())


def local_records(conn):
    """Everything the user recorded through this system, newest first."""
    statuses = {kind: _statuses(conn, table) for kind, table in STATUS_TABLES.items()}
    placeholders = ",".join("?" for _ in NOT_RECORDS)
    rows = conn.execute(f"""
        SELECT note_id, intent, processed_at, original_text, summary,
               scheduled_at, recurrence, notification_text, event_title,
               event_start, event_location, persistent_task_text
        FROM ai_results
        WHERE intent NOT IN ({placeholders})
        ORDER BY processed_at DESC
    """, NOT_RECORDS).fetchall()

    records = []
    for (note_id, intent, processed_at, original_text, summary, scheduled_at,
         recurrence, notification_text, event_title, event_start,
         event_location, persistent_task_text) in rows:
        record = {
            "id": note_id,
            "kind": KIND_LABELS.get(intent, intent),
            "recorded_at": _local(processed_at),
            "text": (original_text or summary or "")[:MAX_RECORD_TEXT_CHARS],
        }
        optional = {
            "summary": summary if summary and summary != original_text else None,
            "scheduled_at": _local(scheduled_at),
            "recurrence": recurrence,
            "notification_text": notification_text,
            "event_title": event_title,
            "start": event_start,
            "location": event_location,
            "task": persistent_task_text,
            "status": statuses.get(intent, {}).get(note_id),
        }
        record.update({key: value for key, value in optional.items() if value})
        records.append(record)

    for job_id, objective, result_text, completed_at, created_at in conn.execute("""
        SELECT id, objective, result_text, completed_at, created_at
        FROM research_jobs WHERE result_text IS NOT NULL
    """):
        records.append({
            "id": f"research-{job_id}",
            "kind": KIND_LABELS["research"],
            "recorded_at": _local(completed_at or created_at),
            "text": objective,
            "result": (result_text or "")[:MAX_RECORD_TEXT_CHARS],
        })

    for task_text, status, group_name, source_note_id in conn.execute("""
        SELECT task_text, status, group_name, source_note_id FROM persistent_reminders
    """):
        for record in records:
            if record["id"] == source_note_id:
                record["status"] = status
                if group_name:
                    record["group"] = group_name

    for monitor_id, request_text, status, found_url, created_at in conn.execute("""
        SELECT id, request_text, status, found_url, created_at FROM web_monitors
    """):
        record = {
            "id": f"monitor-{monitor_id}",
            "kind": KIND_LABELS["web_monitor"],
            "recorded_at": _local(created_at),
            "text": request_text,
            "status": status,
        }
        if found_url:
            record["found_url"] = found_url
        records.append(record)

    records.sort(key=lambda record: record.get("recorded_at") or "", reverse=True)
    return records


def fetch_calendar_events(now, credentials=None, session=requests):
    credentials = credentials or load_webhook_credentials()
    if not credentials:
        raise RuntimeError("Apps Scriptカレンダー設定がありません")
    response = session.post(
        credentials["endpoint_url"],
        json={
            "action": "upcoming",
            "secret": credentials["secret"],
            "range_start": (now - timedelta(days=CALENDAR_PAST_DAYS)).isoformat(),
            "range_end": (now + timedelta(days=CALENDAR_FUTURE_DAYS)).isoformat(),
        },
        timeout=60,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "カレンダーの取得に失敗しました")
    return result.get("events") or []


def calendar_records(events):
    records = []
    for index, event in enumerate(events):
        all_day = bool(event.get("all_day"))
        record = {
            "id": f"calendar-{index}",
            "kind": KIND_LABELS["calendar_event"],
            "text": event.get("title") or "無題の予定",
            "start": event.get("start") if all_day else _local(event.get("start")),
            "end": event.get("end") if all_day else _local(event.get("end")),
        }
        if all_day:
            record["all_day"] = True
        if event.get("location"):
            record["location"] = event["location"]
        records.append(record)
    return records


def fit_budget(records, budget=MAX_RECORD_CHARS):
    """Keep records in order until the character budget is spent."""
    kept, used = [], 0
    for record in records:
        size = len(json.dumps(record, ensure_ascii=False))
        if used + size > budget:
            break
        kept.append(record)
        used += size
    return kept


def format_sources(source_ids, records):
    by_id = {record["id"]: record for record in records}
    lines = []
    for source_id in source_ids:
        record = by_id.get(source_id)
        if record is None:
            continue
        when = record.get("recorded_at") or record.get("start") or ""
        text = " ".join(str(record["text"]).split())
        if len(text) > 40:
            text = text[:40] + "…"
        lines.append(f"・{record['kind']} {str(when)[:10]}「{text}」")
    return lines


def answer_question(conn, question, now=None, ask=ask_llm, fetch_events=fetch_calendar_events):
    """Return {"found": bool, "text": str} ready to send as a notification."""
    now = now or datetime.now(LOCAL_TIMEZONE)
    calendar_available = True
    try:
        events = calendar_records(fetch_events(now))
    except Exception as error:
        print("カレンダーを取得できませんでした（記録だけで回答します）:", error)
        events, calendar_available = [], False

    # Calendar events are few and bounded by the date range, so the budget is
    # applied to the local records, which grow without limit.
    records = events + fit_budget(local_records(conn))
    result = ask(
        ANSWER_PROMPT,
        {
            "current_datetime": now.isoformat(),
            "calendar_available": calendar_available,
            "question": question,
            "records": records,
        },
        ANSWER_SCHEMA,
    )
    found = bool(result.get("found"))
    text = (result.get("answer") or "").strip() or "記録には見当たりません。"
    sources = format_sources(result.get("source_ids") or [], records) if found else []
    if sources:
        text += "\n\n根拠:\n" + "\n".join(sources[:4])
    return {"found": found, "text": text}
