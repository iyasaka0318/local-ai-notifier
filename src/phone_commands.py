"""Send a command from this PC to the phone.

The phone's ntfy app rebroadcasts every message it receives to other apps
(io.heckel.ntfy.MESSAGE_RECEIVED), and an Automate flow acts on the ones from
this topic. The topic name is the only thing protecting an ntfy topic, so each
command also carries a key the flow checks before it does anything.
"""

import json
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from project_paths import CONFIG_DIR


NTFY_URL = "https://ntfy.sh"
KEY_PATH = Path(CONFIG_DIR) / "phone_command.json"
LOCAL_TIMEZONE = ZoneInfo("Asia/Tokyo")
# The clock app takes a time of day and rings at its next occurrence, so a
# command must reach the phone less than a day before the alarm.
ALARM_DIRECT_WINDOW = timedelta(hours=23)
ALARM_LEAD = timedelta(hours=12)
# A claim older than this belongs to a worker that died between claiming and
# sending; the command goes back in the queue rather than staying lost.
STALE_CLAIM = timedelta(minutes=5)


def phone_topic(topic):
    configured = os.environ.get("NTFY_PHONE_TOPIC", "").strip()
    return configured or f"{topic}-phone"


def load_key(path=KEY_PATH, create=False):
    path = Path(path)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["key"]
    if not create:
        raise RuntimeError(
            "スマホ連携の鍵がありません。scripts/phone_command_setup.py を実行してください"
        )
    key = secrets.token_urlsafe(18)
    path.write_text(json.dumps({"key": key}), encoding="utf-8")
    path.chmod(0o600)
    return key


def build_command(action, key, **fields):
    return {"v": 1, "key": key, "action": action, **fields}


def send_phone_command(topic, action, session=requests, key=None, **fields):
    """Publish one command. Raises when ntfy does not accept it."""
    command = build_command(action, key or load_key(), **fields)
    response = session.post(
        NTFY_URL,
        json={
            "topic": phone_topic(topic),
            "message": json.dumps(command, ensure_ascii=False),
            # The flow consumes it; the user should not see a popup for it.
            "priority": 1,
        },
        timeout=30,
    )
    response.raise_for_status()
    return command


def _parse(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed


def next_alarm_time(scheduled_at, now):
    """The alarm moment, moved to the next day while it lies in the past.

    "7時にアラーム" spoken at night is resolved to today's 7:00 often enough
    that failing on it would be wrong; an alarm is about the next 7:00.
    """
    alarm_time = _parse(scheduled_at).astimezone(LOCAL_TIMEZONE)
    while alarm_time <= now:
        alarm_time += timedelta(days=1)
    return alarm_time


def alarm_command(scheduled_at, label, now=None):
    """Return (payload, send_after, alarm_time) for one alarm."""
    now = now or datetime.now(LOCAL_TIMEZONE)
    alarm_time = next_alarm_time(scheduled_at, now)
    payload = {
        "seconds": alarm_time.hour * 3600 + alarm_time.minute * 60,
        "label": (label or "アラーム").strip()[:60],
        "at": alarm_time.isoformat(),
    }
    if alarm_time - now <= ALARM_DIRECT_WINDOW:
        send_after = now
    else:
        send_after = alarm_time - ALARM_LEAD
    return payload, send_after, alarm_time


def queue_phone_command(cursor, item_id, source_note_id, action, payload, send_after, now=None):
    now = now or datetime.now(LOCAL_TIMEZONE)
    cursor.execute("""
        INSERT INTO phone_commands (
            note_id, source_note_id, action, payload, send_after, status, created_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(note_id) DO UPDATE SET
            source_note_id = excluded.source_note_id,
            action = excluded.action,
            payload = excluded.payload,
            send_after = excluded.send_after,
            status = 'pending', attempts = 0, last_error = NULL, sent_at = NULL
    """, (
        item_id, source_note_id, action,
        json.dumps(payload, ensure_ascii=False), send_after.isoformat(), now.isoformat(),
    ))


def cancel_phone_command(conn, item_id):
    """Return "cancelled", "sent" (too late: the phone already has it) or None."""
    row = conn.execute(
        "SELECT status FROM phone_commands WHERE note_id = ?", (item_id,)
    ).fetchone()
    if row is None:
        return None
    if row[0] in ("sent", "sending"):
        return "sent"
    conn.execute(
        "UPDATE phone_commands SET status = 'cancelled' WHERE note_id = ?", (item_id,)
    )
    return "cancelled"


def alarm_summary(alarms):
    """One line per alarm, for the confirmation the phone shows after setting them."""
    lines = []
    for alarm in sorted(alarms, key=lambda alarm: alarm.get("at") or ""):
        at = _parse(alarm["at"]).astimezone(LOCAL_TIMEZONE)
        line = f"{at.month}/{at.day} {at:%H:%M}"
        if alarm.get("label") and alarm["label"] != "アラーム":
            line += f" {alarm['label']}"
        lines.append(line)
    return "\n".join(lines)


def _batches(rows):
    """Group due alarms into one command; everything else goes out alone.

    The phone's flow only listens between commands, so three alarms sent as
    three messages a moment apart can lose one. One message carries them all.
    """
    alarms = [row for row in rows if row[1] == "alarm_add"]
    others = [row for row in rows if row[1] != "alarm_add"]
    batches = [([row[0]], row[1], json.loads(row[2])) for row in others]
    if alarms:
        payloads = sorted(
            (json.loads(row[2]) for row in alarms), key=lambda alarm: alarm.get("at") or ""
        )
        fields = {
            "alarms": [
                {"seconds": alarm["seconds"], "label": alarm["label"]} for alarm in payloads
            ],
            "summary": alarm_summary(payloads),
            # The first alarm is repeated at the top level for a flow that
            # predates the list.
            "seconds": payloads[0]["seconds"],
            "label": payloads[0]["label"],
        }
        batches.append(([row[0] for row in alarms], "alarm_add", fields))
    return batches


def dispatch_phone_commands(conn, topic, now=None, sender=send_phone_command):
    """Send every command that is due. Returns the number of commands delivered."""
    now = now or datetime.now(LOCAL_TIMEZONE)
    sent = 0
    conn.execute("""
        UPDATE phone_commands SET status = 'pending'
        WHERE status = 'sending' AND claimed_at < ?
    """, ((now - STALE_CLAIM).isoformat(),))
    conn.commit()
    rows = conn.execute("""
        SELECT note_id, action, payload, send_after FROM phone_commands
        WHERE status = 'pending'
    """).fetchall()
    due = []
    for row in rows:
        if _parse(row[3]) > now:
            continue
        # Claim before sending so two workers cannot both deliver it.
        claimed = conn.execute("""
            UPDATE phone_commands
            SET status = 'sending', attempts = attempts + 1, claimed_at = ?
            WHERE note_id = ? AND status = 'pending'
        """, (now.isoformat(), row[0]))
        conn.commit()
        if claimed.rowcount:
            due.append(row)

    for item_ids, action, fields in _batches(due):
        placeholders = ",".join("?" for _ in item_ids)
        try:
            sender(topic, action, **fields)
        except Exception as error:
            conn.execute(f"""
                UPDATE phone_commands SET status = 'pending', last_error = ?
                WHERE note_id IN ({placeholders}) AND status = 'sending'
            """, (str(error)[:2000], *item_ids))
            conn.commit()
            continue
        conn.execute(f"""
            UPDATE phone_commands SET status = 'sent', sent_at = ?, last_error = NULL
            WHERE note_id IN ({placeholders})
        """, (now.isoformat(), *item_ids))
        conn.commit()
        sent += len(item_ids)
    return sent
