import hashlib
import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from urllib.parse import quote

import requests

from automation_config import CALENDAR_DEFAULT_MINUTES, CALENDAR_TIMEZONE
from failure_notifier import notify_processing_failure
from instance_lock import SingleInstanceLock
from keep_client import get_authenticated_keep
from project_paths import CONFIG_DIR, DB_PATH, RUNTIME_DIR, ensure_runtime_directories
from tasks_client import get_tasks_inbox_client
from reminder_worker import parse_scheduled_at
from state_store import ensure_schema, mark_note_processed, utc_now


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.environ.get(
    "GOOGLE_CALENDAR_TOKEN_FILE",
    str(CONFIG_DIR / "google_calendar_token.json"),
)
WEBHOOK_CONFIG_FILE = os.environ.get(
    "GOOGLE_CALENDAR_WEBHOOK_CONFIG_FILE",
    str(CONFIG_DIR / "calendar_webhook.json"),
)
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_ROOT = "https://www.googleapis.com/calendar/v3"


class MissingCalendarCredentials(RuntimeError):
    pass


def load_webhook_credentials():
    endpoint_url = os.environ.get("GOOGLE_CALENDAR_WEBHOOK_URL")
    secret = os.environ.get("GOOGLE_CALENDAR_WEBHOOK_SECRET")
    if endpoint_url and secret:
        return {"endpoint_url": endpoint_url, "secret": secret}
    if not os.path.exists(WEBHOOK_CONFIG_FILE):
        return None
    with open(WEBHOOK_CONFIG_FILE, encoding="utf-8") as file:
        credentials = json.load(file)
    if not credentials.get("endpoint_url") or not credentials.get("secret"):
        raise MissingCalendarCredentials(
            "Apps Scriptカレンダー設定に必要な値がありません"
        )
    return credentials


def load_credentials():
    credentials = {
        "client_id": os.environ.get("GOOGLE_CALENDAR_CLIENT_ID"),
        "client_secret": os.environ.get("GOOGLE_CALENDAR_CLIENT_SECRET"),
        "refresh_token": os.environ.get("GOOGLE_CALENDAR_REFRESH_TOKEN"),
        "token_uri": TOKEN_URL,
    }
    if all(credentials[key] for key in ("client_id", "client_secret", "refresh_token")):
        return credentials

    if not os.path.exists(TOKEN_FILE):
        raise MissingCalendarCredentials(
            "Google Calendarの初回認証が必要です"
        )
    with open(TOKEN_FILE, encoding="utf-8") as file:
        credentials = json.load(file)
    if not all(credentials.get(key) for key in (
        "client_id", "client_secret", "refresh_token"
    )):
        raise MissingCalendarCredentials(
            "Google Calendar認証ファイルに必要な値がありません"
        )
    return credentials


def get_access_token(session=requests):
    credentials = load_credentials()
    response = session.post(
        credentials.get("token_uri") or TOKEN_URL,
        data={
            "client_id": credentials["client_id"],
            "client_secret": credentials["client_secret"],
            "refresh_token": credentials["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    response.raise_for_status()
    access_token = response.json().get("access_token")
    if not access_token:
        raise RuntimeError("Google Calendarのアクセストークンを取得できません")
    return access_token


def deterministic_event_id(note_id):
    return "ai" + hashlib.sha256(note_id.encode("utf-8")).hexdigest()[:30]


def build_event_body(
    note_id,
    event_title,
    event_start,
    event_end=None,
    all_day=False,
    location=None,
    description=None,
):
    body = {
        "id": deterministic_event_id(note_id),
        "summary": event_title,
        "extendedProperties": {"private": {"sourceNoteId": note_id}},
    }
    if location:
        body["location"] = location
    if description:
        body["description"] = description

    if all_day:
        start_date = date.fromisoformat(event_start[:10])
        end_date = (
            date.fromisoformat(event_end[:10])
            if event_end
            else start_date + timedelta(days=1)
        )
        if end_date <= start_date:
            raise ValueError("終了日は開始日より後である必要があります")
        body["start"] = {"date": start_date.isoformat()}
        body["end"] = {"date": end_date.isoformat()}
    else:
        start_at = parse_scheduled_at(event_start)
        end_at = (
            parse_scheduled_at(event_end)
            if event_end
            else start_at + timedelta(minutes=CALENDAR_DEFAULT_MINUTES)
        )
        if end_at <= start_at:
            raise ValueError("終了時刻は開始時刻より後である必要があります")
        body["start"] = {
            "dateTime": start_at.isoformat(),
            "timeZone": CALENDAR_TIMEZONE,
        }
        body["end"] = {
            "dateTime": end_at.isoformat(),
            "timeZone": CALENDAR_TIMEZONE,
        }
    return body


def create_calendar_event(access_token, calendar_id, body, session=requests):
    calendar_path = quote(calendar_id, safe="")
    headers = {"Authorization": f"Bearer {access_token}"}
    response = session.post(
        f"{API_ROOT}/calendars/{calendar_path}/events",
        params={"sendUpdates": "none"},
        headers=headers,
        json=body,
        timeout=30,
    )
    if response.status_code == 409:
        update_body = dict(body)
        update_body.pop("id", None)
        existing = session.patch(
            f"{API_ROOT}/calendars/{calendar_path}/events/{body['id']}",
            params={"sendUpdates": "none"},
            headers=headers,
            json=update_body,
            timeout=30,
        )
        existing.raise_for_status()
        result = dict(existing.json())
        result.setdefault("id", body["id"])
        return result
    response.raise_for_status()
    return response.json()


def create_calendar_event_via_webhook(credentials, body, session=requests):
    source_note_id = (
        body.get("extendedProperties", {})
        .get("private", {})
        .get("sourceNoteId")
    )
    all_day = "date" in body["start"]
    payload = {
        "secret": credentials["secret"],
        "source_note_id": source_note_id,
        "event_title": body["summary"],
        "event_start": body["start"].get("date") or body["start"].get("dateTime"),
        "event_end": body["end"].get("date") or body["end"].get("dateTime"),
        "all_day": all_day,
        "location": body.get("location"),
        "description": body.get("description"),
    }
    response = session.post(
        credentials["endpoint_url"],
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(
            f"Apps Scriptカレンダー登録に失敗しました: {result.get('error', '不明なエラー')}"
        )
    return {
        "id": result.get("event_id") or body["id"],
        "duplicate": bool(result.get("duplicate")),
    }


def get_keep():
    return get_authenticated_keep()


def finish_source_note(
    conn,
    source_note_id,
    keep_factory=get_keep,
    tasks_factory=get_tasks_inbox_client,
):
    source = conn.execute("""
        SELECT p.content_hash, a.automation_source
        FROM processed_notes AS p
        LEFT JOIN ai_results AS a ON a.note_id = p.note_id
        WHERE p.note_id = ?
    """, (source_note_id,)).fetchone()
    if not source:
        return
    content_hash, automation_source = source
    if automation_source == "explicit_ai_memo":
        keep = keep_factory()
        note = keep.get(source_note_id)
        if note is not None and not note.trashed:
            note.trash()
            keep.sync()
    elif automation_source == "google_tasks":
        tasks_factory().complete_task(source_note_id)
    mark_note_processed(conn, source_note_id, content_hash)


def process_calendar_job(
    conn,
    job,
    token_factory=get_access_token,
    event_creator=create_calendar_event,
    keep_factory=get_keep,
    webhook_credentials_factory=load_webhook_credentials,
    webhook_event_creator=create_calendar_event_via_webhook,
    tasks_factory=get_tasks_inbox_client,
):
    (
        note_id, event_title, event_start, event_end, all_day,
        location, description,
    ) = job
    timestamp = utc_now()
    conn.execute("""
        UPDATE calendar_jobs
        SET status = 'running', last_error = NULL, updated_at = ?
        WHERE note_id = ?
    """, (timestamp, note_id))
    conn.commit()

    body = build_event_body(
        note_id, event_title, event_start, event_end, bool(all_day),
        location, description,
    )
    webhook_credentials = webhook_credentials_factory()
    if webhook_credentials:
        created = webhook_event_creator(webhook_credentials, body)
    else:
        access_token = token_factory()
        created = event_creator(access_token, CALENDAR_ID, body)
    calendar_event_id = created.get("id") or body["id"]

    conn.execute("""
        UPDATE calendar_jobs
        SET calendar_event_id = ?, updated_at = ?
        WHERE note_id = ?
    """, (calendar_event_id, utc_now(), note_id))
    conn.commit()

    finish_source_note(conn, note_id, keep_factory, tasks_factory)
    completed_at = utc_now()
    conn.execute("""
        UPDATE calendar_jobs
        SET status = 'created', calendar_event_id = ?,
            created_in_calendar_at = COALESCE(created_in_calendar_at, ?),
            last_error = NULL, updated_at = ?
        WHERE note_id = ?
    """, (calendar_event_id, completed_at, completed_at, note_id))
    conn.commit()


def main():
    ensure_runtime_directories()
    lock = SingleInstanceLock(str(RUNTIME_DIR / "calendar_worker.lock"))
    if not lock.acquire():
        return
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        ensure_schema(conn)
        jobs = conn.execute("""
            SELECT note_id, event_title, event_start, event_end, all_day,
                   event_location, event_description
            FROM calendar_jobs
            WHERE status IN ('pending', 'retry', 'waiting_auth')
              AND calendar_ready = 1
              AND event_title IS NOT NULL
              AND event_start IS NOT NULL
            ORDER BY created_at
        """).fetchall()
        for job in jobs:
            try:
                process_calendar_job(conn, job)
            except MissingCalendarCredentials as error:
                conn.execute("""
                    UPDATE calendar_jobs
                    SET status = 'waiting_auth', last_error = ?, updated_at = ?
                    WHERE note_id = ?
                """, (str(error), utc_now(), job[0]))
                conn.commit()
                notify_processing_failure(
                    conn,
                    os.environ.get("NTFY_TOPIC"),
                    "Googleカレンダー登録",
                    job[0],
                    error,
                    retrying=False,
                )
            except Exception as error:
                conn.execute("""
                    UPDATE calendar_jobs
                    SET status = 'retry', last_error = ?, updated_at = ?
                    WHERE note_id = ?
                """, (str(error)[:2000], utc_now(), job[0]))
                conn.commit()
                notify_processing_failure(
                    conn,
                    os.environ.get("NTFY_TOPIC"),
                    "Googleカレンダー登録",
                    job[0],
                    error,
                    retrying=True,
                )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
