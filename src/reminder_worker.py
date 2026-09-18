import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from failure_notifier import notify_processing_failure
from instance_lock import SingleInstanceLock
from project_paths import DB_PATH, RUNTIME_DIR, ensure_runtime_directories
from report_notifier import send_execution_report
from state_store import drain_remote_deletes, drain_reports, ensure_schema, utc_now


NTFY_URL = "https://ntfy.sh"
LOCAL_TIMEZONE = ZoneInfo("Asia/Tokyo")


def parse_scheduled_at(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed


def send_notification(topic, message, title="リマインダー"):
    response = requests.post(
        NTFY_URL,
        json={
            "topic": topic,
            "title": title[:120],
            "message": message[:3500],
            "priority": 4,
        },
        timeout=30,
    )
    response.raise_for_status()


def latest_persistent_slot(now):
    local_now = now.astimezone(LOCAL_TIMEZONE)
    eleven = local_now.replace(hour=11, minute=0, second=0, microsecond=0)
    twenty_three = local_now.replace(hour=23, minute=0, second=0, microsecond=0)
    if local_now >= twenty_three:
        return twenty_three
    if local_now >= eleven:
        return eleven
    return None


def group_persistent_tasks(rows):
    """Bundle grouped tasks together; ungrouped tasks stay one per bundle.

    Sending "買い物: 牛乳 / パン / 卵" as one notification instead of three is
    the whole point of grouping, so the delivery unit has to be the bundle.
    """
    bundles = []
    by_group = {}
    for row in rows:
        group_name = row[4]
        if not group_name:
            bundles.append((None, [row]))
            continue
        if group_name not in by_group:
            by_group[group_name] = []
            bundles.append((group_name, by_group[group_name]))
        by_group[group_name].append(row)
    return bundles


def format_group_message(group_name, task_texts):
    if len(task_texts) == 1 and not group_name:
        return task_texts[0]
    lines = "\n".join(f"・{text}" for text in task_texts)
    if group_name:
        return f"{group_name}\n{lines}"
    return lines


def dispatch_persistent_reminders(conn, topic, now=None, sender=send_notification):
    now = now or datetime.now(LOCAL_TIMEZONE)
    slot = latest_persistent_slot(now)
    if slot is None:
        return 0

    rows = conn.execute("""
        SELECT id, task_text, created_at, last_notified_slot, group_name
        FROM persistent_reminders
        WHERE status = 'active'
        ORDER BY group_name IS NULL, group_name, created_at, id
    """).fetchall()

    sent = 0
    for group_name, bundle in group_persistent_tasks(rows):
        claimed = []
        for task_id, task_text, created_at, last_notified_slot, _group in bundle:
            try:
                created = parse_scheduled_at(created_at)
            except (TypeError, ValueError):
                created = now
            if created.astimezone(LOCAL_TIMEZONE) > slot:
                continue
            if last_notified_slot:
                try:
                    if parse_scheduled_at(last_notified_slot) >= slot:
                        continue
                except (TypeError, ValueError):
                    pass

            cursor = conn.execute("""
                UPDATE persistent_reminders
                SET last_notified_slot = ?, updated_at = ?
                WHERE id = ? AND status = 'active' AND last_notified_slot IS ?
            """, (slot.isoformat(), utc_now(), task_id, last_notified_slot))
            conn.commit()
            if cursor.rowcount:
                claimed.append((task_id, task_text, last_notified_slot))

        if not claimed:
            continue

        message = format_group_message(group_name, [text for _, text, _ in claimed])
        title = group_name or "リマインダー"
        try:
            sender(topic, message, title)
        except Exception as error:
            for task_id, _text, previous_slot in claimed:
                conn.execute("""
                    UPDATE persistent_reminders
                    SET last_notified_slot = ?, last_error = ?, updated_at = ?
                    WHERE id = ? AND status = 'active' AND last_notified_slot = ?
                """, (
                    previous_slot,
                    str(error)[:2000],
                    utc_now(),
                    task_id,
                    slot.isoformat(),
                ))
            conn.commit()
            notify_processing_failure(
                conn, topic, "継続リマインダー通知", claimed[0][0], error,
                retrying=True, sender=sender,
            )
            continue

        for task_id, _text, _previous in claimed:
            conn.execute("""
                UPDATE persistent_reminders
                SET last_error = NULL, updated_at = ?
                WHERE id = ? AND status = 'active' AND last_notified_slot = ?
            """, (utc_now(), task_id, slot.isoformat()))
        conn.commit()
        sent += 1
    return sent


def dispatch_persistent_now(
    conn,
    topic,
    command_note_id,
    sender=send_notification,
    now=None,
):
    now = now or datetime.now(LOCAL_TIMEZONE)
    current_slot = latest_persistent_slot(now)
    command = conn.execute("""
        SELECT status FROM persistent_reminder_commands
        WHERE note_id = ? AND action = 'notify_now'
    """, (command_note_id,)).fetchone()
    if not command:
        raise ValueError("今すぐ通知するコマンドが登録されていません")
    if command[0] == "completed":
        return 0

    delivered = {
        row[0] for row in conn.execute("""
            SELECT task_id FROM persistent_reminder_deliveries
            WHERE command_note_id = ?
        """, (command_note_id,))
    }
    tasks = conn.execute("""
        SELECT id, task_text FROM persistent_reminders
        WHERE status = 'active' ORDER BY created_at, id
    """).fetchall()

    sent = 0
    try:
        if not tasks and -1 not in delivered:
            sender(
                topic,
                "現在、保存されているリマインドタスクはありません。",
                "リマインダー",
            )
            conn.execute("""
                INSERT OR IGNORE INTO persistent_reminder_deliveries (
                    command_note_id, task_id, delivered_at
                ) VALUES (?, -1, ?)
            """, (command_note_id, utc_now()))
            conn.commit()
            sent += 1

        for task_id, task_text in tasks:
            if task_id in delivered:
                continue
            sender(topic, task_text, "リマインダー")
            conn.execute("""
                INSERT OR IGNORE INTO persistent_reminder_deliveries (
                    command_note_id, task_id, delivered_at
                ) VALUES (?, ?, ?)
            """, (command_note_id, task_id, utc_now()))
            if current_slot is not None:
                created_at = conn.execute(
                    "SELECT created_at FROM persistent_reminders WHERE id = ?",
                    (task_id,),
                ).fetchone()[0]
                if parse_scheduled_at(created_at).astimezone(LOCAL_TIMEZONE) <= current_slot:
                    conn.execute("""
                        UPDATE persistent_reminders
                        SET last_notified_slot = ?, last_error = NULL, updated_at = ?
                        WHERE id = ?
                    """, (current_slot.isoformat(), utc_now(), task_id))
            conn.commit()
            sent += 1
    except Exception as error:
        conn.execute("""
            UPDATE persistent_reminder_commands
            SET status = 'pending', last_error = ?, updated_at = ?
            WHERE note_id = ?
        """, (str(error)[:2000], utc_now(), command_note_id))
        conn.commit()
        raise

    conn.execute("""
        UPDATE persistent_reminder_commands
        SET status = 'completed', last_error = NULL, updated_at = ?
        WHERE note_id = ?
    """, (utc_now(), command_note_id))
    conn.commit()
    return sent


def dispatch_due_reminders(conn, topic, now=None, sender=send_notification):
    now = now or datetime.now(LOCAL_TIMEZONE)
    stale_sending_before = (now - timedelta(minutes=5)).isoformat()
    conn.execute("""
        UPDATE reminders
        SET status = 'pending', updated_at = ?
        WHERE status = 'sending'
          AND (updated_at IS NULL OR updated_at < ?)
    """, (now.isoformat(), stale_sending_before))
    conn.commit()
    rows = conn.execute("""
        SELECT note_id, title, summary, scheduled_at, recurrence, source_type
        FROM reminders
        WHERE status = 'pending' AND scheduled_at IS NOT NULL
        ORDER BY scheduled_at
    """).fetchall()
    sent = 0

    for note_id, title, summary, scheduled_at, recurrence, source_type in rows:
        try:
            due_at = parse_scheduled_at(scheduled_at)
        except (TypeError, ValueError) as error:
            conn.execute("""
                UPDATE reminders
                SET status = 'waiting_information', last_error = ?, updated_at = ?
                WHERE note_id = ?
            """, (f"invalid scheduled_at: {error}", utc_now(), note_id))
            conn.commit()
            notify_processing_failure(
                conn, topic, "日時指定リマインダー", note_id, error,
                retrying=False, sender=sender,
            )
            continue

        if due_at > now.astimezone(due_at.tzinfo):
            continue

        claimed_at = utc_now()
        cursor = conn.execute("""
            UPDATE reminders
            SET status = 'sending', last_error = NULL, updated_at = ?
            WHERE note_id = ? AND status = 'pending'
        """, (claimed_at, note_id))
        conn.commit()
        if cursor.rowcount == 0:
            continue

        try:
            notification_title = (
                title if source_type == "research" and title else "リマインダー"
            )
            sender(topic, summary, notification_title)
        except Exception as error:
            conn.execute("""
                UPDATE reminders
                SET status = 'pending', last_error = ?, updated_at = ?
                WHERE note_id = ? AND status = 'sending'
            """, (str(error)[:2000], utc_now(), note_id))
            conn.commit()
            notify_processing_failure(
                conn, topic, "日時指定リマインダー通知", note_id, error,
                retrying=True, sender=sender,
            )
            continue

        timestamp = utc_now()
        recurrence = (recurrence or "").lower()
        if recurrence == "daily":
            next_at = due_at + timedelta(days=1)
        elif recurrence.startswith("weekly:"):
            next_at = due_at + timedelta(days=7)
        else:
            next_at = None

        if next_at is None:
            final_cursor = conn.execute("""
                UPDATE reminders
                SET status = 'notified', notified_at = ?, last_error = NULL,
                    updated_at = ?
                WHERE note_id = ? AND status = 'sending'
            """, (timestamp, timestamp, note_id))
        else:
            step = timedelta(days=1 if recurrence == "daily" else 7)
            while next_at <= now.astimezone(next_at.tzinfo):
                next_at += step
            final_cursor = conn.execute("""
                UPDATE reminders
                SET status = 'pending', scheduled_at = ?, notified_at = ?,
                    last_error = NULL, updated_at = ?
                WHERE note_id = ? AND status = 'sending'
            """, (next_at.isoformat(), timestamp, timestamp, note_id))
        conn.commit()
        if final_cursor.rowcount:
            sent += 1

    return sent


def main():
    ensure_runtime_directories()
    instance_lock = SingleInstanceLock(
        str(RUNTIME_DIR / "reminder_worker.lock")
    )
    if not instance_lock.acquire():
        return

    topic = os.environ["NTFY_TOPIC"]
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        ensure_schema(conn)
        # This worker runs every minute, so it is the natural retry cadence for
        # anything that could not be delivered when it happened.
        delivered, _failed = drain_reports(
            conn, lambda entries: send_execution_report(topic, entries)
        )
        if delivered:
            print(f"Re-sent {delivered} execution report(s)")
        sent = dispatch_due_reminders(conn, topic)
        sent += dispatch_persistent_reminders(conn, topic)
    finally:
        conn.close()
    if sent:
        print(f"Sent {sent} reminder notification(s)")


if __name__ == "__main__":
    main()
