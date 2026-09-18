import hashlib
import os
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from ai_memo import trash_processed_ai_memo
from failure_notifier import notify_processing_failure
from calendar_worker import load_webhook_credentials
from instance_lock import SingleInstanceLock
from keep_client import get_authenticated_keep
from project_paths import DB_PATH, RUNTIME_DIR, ensure_runtime_directories
from reminder_worker import latest_persistent_slot, parse_scheduled_at, send_notification
from state_store import ensure_schema, mark_note_processed, utc_now
from tasks_client import get_tasks_inbox_client


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_TIMEZONE = ZoneInfo("Asia/Tokyo")
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
MAEBASHI_LATITUDE = 36.3895
MAEBASHI_LONGITUDE = 139.0634


def weather_label(code):
    code = int(code or 0)
    if code == 0:
        return "晴れ"
    if code in (1, 2):
        return "晴れ時々曇り"
    if code in (3, 45, 48):
        return "曇り"
    if code in (51, 53, 55, 56, 57):
        return "霧雨"
    if code in (61, 63, 65, 66, 67, 80, 81, 82):
        return "雨"
    if code in (71, 73, 75, 77, 85, 86):
        return "雪"
    if code in (95, 96, 99):
        return "雷雨"
    return "変わりやすい天気"


def is_wet(code, precipitation, probability):
    return (
        weather_label(code) in ("霧雨", "雨", "雪", "雷雨")
        or float(precipitation or 0) >= 0.1
        or int(probability or 0) >= 50
    )


def fetch_weather(session=requests):
    response = session.get(
        WEATHER_URL,
        params={
            "latitude": MAEBASHI_LATITUDE,
            "longitude": MAEBASHI_LONGITUDE,
            "current": "temperature_2m,precipitation,rain,weather_code",
            "hourly": (
                "temperature_2m,precipitation_probability,"
                "precipitation,rain,weather_code"
            ),
            "forecast_hours": 16,
            "timezone": "Asia/Tokyo",
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def build_weather_notification(data, now=None):
    now = (now or datetime.now(LOCAL_TIMEZONE)).astimezone(LOCAL_TIMEZONE)
    current = data.get("current") or {}
    current_code = current.get("weather_code", 0)
    current_label = weather_label(current_code)
    current_temperature = current.get("temperature_2m")
    current_wet = is_wet(
        current_code,
        current.get("precipitation", 0),
        0,
    )

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    codes = hourly.get("weather_code") or []
    probabilities = hourly.get("precipitation_probability") or []
    precipitation = hourly.get("precipitation") or []
    temperatures = hourly.get("temperature_2m") or []
    horizon = now + timedelta(hours=15)
    forecast = []
    for index, value in enumerate(times):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
        if now <= parsed <= horizon:
            forecast.append({
                "time": parsed,
                "code": codes[index],
                "probability": probabilities[index],
                "precipitation": precipitation[index],
                "temperature": temperatures[index],
            })

    wet_hours = [
        item for item in forecast
        if is_wet(item["code"], item["precipitation"], item["probability"])
    ]
    max_probability = max(
        (int(item["probability"] or 0) for item in forecast),
        default=0,
    )
    dominant = "変わりやすい天気"
    if forecast:
        dominant = Counter(weather_label(item["code"]) for item in forecast).most_common(1)[0][0]

    temperature_text = ""
    if current_temperature is not None:
        temperature_text = f"、気温は{round(float(current_temperature), 1)}度"
    lines = [f"前橋市の天気です。現在は{current_label}{temperature_text}です。"]

    if current_wet:
        if wet_hours:
            last_wet = wet_hours[-1]["time"]
            later = [item for item in forecast if item["time"] > last_wet]
            if later:
                later_label = Counter(
                    weather_label(item["code"]) for item in later
                ).most_common(1)[0][0]
                lines.append(
                    f"雨や雪の可能性は{last_wet:%H時}ごろまでで、"
                    f"その後はおおむね{later_label}の見込みです。"
                )
            else:
                lines.append("今後15時間も雨や雪の可能性があります。")
        else:
            lines.append(f"現在の降水はまもなく弱まり、その後はおおむね{dominant}の見込みです。")
    elif wet_hours:
        first_wet = wet_hours[0]["time"]
        probability_text = (
            f"（最大降水確率{max_probability}%）"
            if max_probability >= 30
            else ""
        )
        lines.append(
            f"今は降っていませんが、{first_wet:%H時}ごろから降水の可能性があります"
            f"{probability_text}。"
        )
    else:
        lines.append(f"今後15時間は雨の可能性は低く、おおむね{dominant}の見込みです。")

    if temperatures:
        available = [float(value) for value in temperatures if value is not None]
        if available:
            lines.append(
                f"今後15時間の気温は、およそ{round(min(available), 1)}～"
                f"{round(max(available), 1)}度です。"
            )
    return "\n".join(lines)


def fetch_upcoming_events(now, credentials=None, session=requests):
    credentials = credentials or load_webhook_credentials()
    if not credentials:
        raise RuntimeError("Apps Scriptカレンダー設定がありません")
    response = session.post(
        credentials["endpoint_url"],
        json={
            "action": "upcoming",
            "secret": credentials["secret"],
            "range_start": now.isoformat(),
            "range_end": (now + timedelta(hours=24)).isoformat(),
        },
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(
            f"カレンダー予定の取得に失敗しました: {result.get('error', '不明なエラー')}"
        )
    return result.get("events") or []


def format_calendar_event(event, now):
    title = (event.get("title") or "無題の予定").strip()
    if event.get("all_day"):
        event_date = date.fromisoformat(event["start"][:10])
        if event_date == now.date():
            timing = "今日・終日"
        elif event_date == (now + timedelta(days=1)).date():
            timing = "明日・終日"
        else:
            timing = f"{event_date.month}月{event_date.day}日・終日"
    else:
        start = parse_scheduled_at(event["start"]).astimezone(LOCAL_TIMEZONE)
        if start <= now:
            timing = "現在開催中"
        elif start.date() == now.date():
            timing = f"今日 {start:%H:%M}"
        elif start.date() == (now + timedelta(days=1)).date():
            timing = f"明日 {start:%H:%M}"
        else:
            timing = f"{start.month}月{start.day}日 {start:%H:%M}"
    message = f"{timing}\n{title}"
    if event.get("location"):
        message += f"\n場所: {event['location']}"
    return message


def delivery_exists(conn, note_id, delivery_key):
    return conn.execute("""
        SELECT 1 FROM wake_deliveries
        WHERE note_id = ? AND delivery_key = ?
    """, (note_id, delivery_key)).fetchone() is not None


def record_delivery(conn, note_id, delivery_key):
    conn.execute("""
        INSERT OR IGNORE INTO wake_deliveries (
            note_id, delivery_key, delivered_at
        ) VALUES (?, ?, ?)
    """, (note_id, delivery_key, utc_now()))
    conn.commit()


def event_delivery_key(event):
    raw = f"{event.get('id', '')}\0{event.get('start', '')}"
    return "calendar:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def finish_source_note(
    conn,
    note_id,
    keep_factory=get_authenticated_keep,
    tasks_factory=get_tasks_inbox_client,
):
    source = conn.execute("""
        SELECT p.content_hash, a.automation_source
        FROM processed_notes AS p
        LEFT JOIN ai_results AS a ON a.note_id = p.note_id
        WHERE p.note_id = ?
    """, (note_id,)).fetchone()
    if not source:
        return
    content_hash, automation_source = source
    if automation_source == "explicit_ai_memo":
        keep = keep_factory()
        note = keep.get(note_id)
        if note is not None and not note.trashed:
            trash_processed_ai_memo(keep, note)
    elif automation_source == "google_tasks":
        tasks_factory().complete_task(note_id)
    mark_note_processed(conn, note_id, content_hash)


def process_wake_job(
    conn,
    note_id,
    topic,
    now=None,
    weather_fetcher=fetch_weather,
    calendar_fetcher=fetch_upcoming_events,
    sender=send_notification,
    keep_factory=get_authenticated_keep,
    tasks_factory=get_tasks_inbox_client,
):
    now = (now or datetime.now(LOCAL_TIMEZONE)).astimezone(LOCAL_TIMEZONE)
    conn.execute("""
        UPDATE wake_jobs SET status = 'running', last_error = NULL, updated_at = ?
        WHERE note_id = ?
    """, (utc_now(), note_id))
    conn.commit()

    if not delivery_exists(conn, note_id, "weather"):
        weather_text = build_weather_notification(weather_fetcher(), now)
        sender(topic, weather_text, "起床後の天気")
        record_delivery(conn, note_id, "weather")

    if not delivery_exists(conn, note_id, "calendar:scan-complete"):
        events = calendar_fetcher(now)
        for event in events:
            key = event_delivery_key(event)
            if delivery_exists(conn, note_id, key):
                continue
            sender(topic, format_calendar_event(event, now), "24時間以内の予定")
            record_delivery(conn, note_id, key)
        record_delivery(conn, note_id, "calendar:scan-complete")

    if not delivery_exists(conn, note_id, "tasks:scan-complete"):
        tasks = conn.execute("""
            SELECT id, task_text, created_at
            FROM persistent_reminders
            WHERE status = 'active'
            ORDER BY created_at, id
        """).fetchall()
        current_slot = latest_persistent_slot(now)
        for task_id, task_text, created_at in tasks:
            key = f"task:{task_id}"
            if delivery_exists(conn, note_id, key):
                continue
            sender(topic, task_text, "リマインダー")
            record_delivery(conn, note_id, key)
            if (
                current_slot is not None
                and parse_scheduled_at(created_at).astimezone(LOCAL_TIMEZONE)
                <= current_slot
            ):
                conn.execute("""
                    UPDATE persistent_reminders
                    SET last_notified_slot = ?, last_error = NULL, updated_at = ?
                    WHERE id = ?
                """, (current_slot.isoformat(), utc_now(), task_id))
                conn.commit()
        record_delivery(conn, note_id, "tasks:scan-complete")

    finish_source_note(conn, note_id, keep_factory, tasks_factory)
    completed_at = utc_now()
    conn.execute("""
        UPDATE wake_jobs
        SET status = 'completed', completed_at = ?, updated_at = ?, last_error = NULL
        WHERE note_id = ?
    """, (completed_at, completed_at, note_id))
    conn.commit()


def main():
    ensure_runtime_directories()
    lock = SingleInstanceLock(str(RUNTIME_DIR / "wake_worker.lock"))
    if not lock.acquire():
        return
    topic = os.environ["NTFY_TOPIC"]
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        ensure_schema(conn)
        jobs = conn.execute("""
            SELECT note_id FROM wake_jobs
            WHERE status IN ('pending', 'retry')
            ORDER BY created_at
        """).fetchall()
        for (note_id,) in jobs:
            try:
                process_wake_job(conn, note_id, topic)
            except Exception as error:
                conn.execute("""
                    UPDATE wake_jobs
                    SET status = 'retry', last_error = ?, updated_at = ?
                    WHERE note_id = ?
                """, (str(error)[:2000], utc_now(), note_id))
                conn.commit()
                notify_processing_failure(
                    conn,
                    topic,
                    "起床時ブリーフィング",
                    note_id,
                    error,
                    retrying=True,
                )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
