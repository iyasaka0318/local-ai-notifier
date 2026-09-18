import hashlib
import json
import sqlite3
from datetime import datetime, timedelta


VALID_PROCESSING_STATUSES = {
    "new", "processing", "waiting_downstream", "processed", "failed"
}
RECOVERABLE_JOB_TABLES = {"research_jobs", "calendar_jobs", "wake_jobs"}

# Work tables keyed by item id rather than by source note id.
ITEM_TABLES = (
    "todos",
    "memos",
    "calendar_jobs",
    "reminders",
    "wake_jobs",
    "web_monitors",
)

ITEM_ID_SEPARATOR = "#"


def utc_now():
    return datetime.now().astimezone().isoformat()


def note_content_hash(title, text):
    payload = f"{title or ''}\0{text or ''}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _columns(conn, table_name):
    return {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table_name})")
    }


def _add_column(conn, table_name, definition):
    column_name = definition.split()[0]
    if column_name not in _columns(conn, table_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {definition}")


def ensure_schema(conn):
    """Create or migrate the local schema without deleting existing data."""
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    if not conn.in_transaction:
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_notes (
            note_id TEXT PRIMARY KEY
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_results (
            note_id TEXT PRIMARY KEY,
            title TEXT,
            original_text TEXT,
            intent TEXT,
            summary TEXT,
            actionable INTEGER,
            calendar_ready INTEGER,
            needs_target_resolution INTEGER,
            needs_confirmation INTEGER,
            missing_information TEXT,
            processed_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS web_monitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id TEXT,
            request_text TEXT NOT NULL,
            search_query TEXT NOT NULL,
            monitor_urls TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            last_checked_at TEXT,
            found_url TEXT
        )
    """)

    _add_column(conn, "processed_notes", "content_hash TEXT")
    _add_column(conn, "processed_notes", "updated_at TEXT")
    _add_column(conn, "processed_notes", "status TEXT NOT NULL DEFAULT 'processed'")
    _add_column(conn, "processed_notes", "last_error TEXT")
    _add_column(conn, "ai_results", "scheduled_at TEXT")
    _add_column(conn, "ai_results", "automation_source TEXT")
    _add_column(conn, "ai_results", "recurrence TEXT")
    _add_column(conn, "ai_results", "actions TEXT")
    _add_column(conn, "ai_results", "notification_text TEXT")
    _add_column(conn, "ai_results", "event_title TEXT")
    _add_column(conn, "ai_results", "event_start TEXT")
    _add_column(conn, "ai_results", "event_end TEXT")
    _add_column(conn, "ai_results", "all_day INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "ai_results", "event_location TEXT")
    _add_column(conn, "ai_results", "event_description TEXT")
    _add_column(conn, "ai_results", "persistent_reminder_action TEXT")
    _add_column(conn, "ai_results", "persistent_task_text TEXT")
    _add_column(conn, "ai_results", "persistent_target_id INTEGER")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS todos (
            note_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            original_text TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calendar_jobs (
            note_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            original_text TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL,
            calendar_ready INTEGER NOT NULL,
            missing_information TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    _add_column(conn, "calendar_jobs", "event_title TEXT")
    _add_column(conn, "calendar_jobs", "event_start TEXT")
    _add_column(conn, "calendar_jobs", "event_end TEXT")
    _add_column(conn, "calendar_jobs", "all_day INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "calendar_jobs", "event_location TEXT")
    _add_column(conn, "calendar_jobs", "event_description TEXT")
    _add_column(conn, "calendar_jobs", "calendar_event_id TEXT")
    _add_column(conn, "calendar_jobs", "last_error TEXT")
    _add_column(conn, "calendar_jobs", "created_in_calendar_at TEXT")
    conn.execute("""
        UPDATE calendar_jobs
        SET status = 'waiting_information'
        WHERE status = 'pending' AND event_start IS NULL
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memos (
            note_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            original_text TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            note_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            original_text TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL,
            scheduled_at TEXT,
            status TEXT NOT NULL,
            notified_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    _add_column(conn, "reminders", "recurrence TEXT")
    _add_column(conn, "reminders", "source_type TEXT")
    _add_column(conn, "reminders", "source_id TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS persistent_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_note_id TEXT NOT NULL UNIQUE,
            task_text TEXT NOT NULL,
            normalized_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            last_notified_slot TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            completed_by_note_id TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_persistent_reminders_status
        ON persistent_reminders (status, created_at)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS persistent_reminder_commands (
            note_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            target_task_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS persistent_reminder_deliveries (
            command_note_id TEXT NOT NULL,
            task_id INTEGER NOT NULL,
            delivered_at TEXT NOT NULL,
            PRIMARY KEY (command_note_id, task_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wake_jobs (
            note_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            last_error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wake_deliveries (
            note_id TEXT NOT NULL,
            delivery_key TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            PRIMARY KEY (note_id, delivery_key)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS research_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_note_id TEXT NOT NULL UNIQUE,
            objective TEXT NOT NULL,
            query TEXT NOT NULL,
            requested_items TEXT NOT NULL,
            execute_at TEXT,
            save_to_keep INTEGER NOT NULL DEFAULT 0,
            notify_mode TEXT NOT NULL DEFAULT 'none',
            notify_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            result_text TEXT,
            source_urls TEXT,
            generated_note_id TEXT,
            notified_at TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        )
    """)
    _add_column(conn, "research_jobs", "notified_at TEXT")
    _add_column(
        conn,
        "research_jobs",
        "notification_content_mode TEXT NOT NULL DEFAULT 'result_summary'",
    )
    _add_column(conn, "research_jobs", "memo_title TEXT")
    _add_column(conn, "research_jobs", "memo_text TEXT")
    _add_column(conn, "research_jobs", "notification_title TEXT")
    _add_column(conn, "research_jobs", "completion_text TEXT")
    _add_column(conn, "research_jobs", "notification_text TEXT")
    _add_column(conn, "research_jobs", "notification_detailed_text TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS generated_notes (
            note_id TEXT PRIMARY KEY,
            source_task_id TEXT NOT NULL,
            source_task_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            created_at TEXT NOT NULL,
            UNIQUE(source_task_id, source_task_type)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS failure_notifications (
            fingerprint TEXT PRIMARY KEY,
            component TEXT NOT NULL,
            item_id TEXT NOT NULL,
            error_type TEXT NOT NULL,
            error_summary TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_attempted_at TEXT,
            last_notified_at TEXT,
            occurrence_count INTEGER NOT NULL DEFAULT 1
        )
    """)
    _add_column(conn, "failure_notifications", "last_attempted_at TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS action_log (
            token TEXT PRIMARY KEY,
            source_note_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            summary TEXT NOT NULL,
            detail TEXT,
            fallback_reason TEXT,
            created_at TEXT NOT NULL,
            undone_at TEXT,
            undo_error TEXT
        )
    """)
    _add_column(conn, "action_log", "note_revision INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "action_log", "target_key TEXT")
    conn.execute(
        "UPDATE action_log SET target_key = item_id WHERE target_key IS NULL"
    )
    _add_column(conn, "processed_notes", "revision INTEGER NOT NULL DEFAULT 0")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_action_log_created
        ON action_log (created_at DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_action_log_item
        ON action_log (item_id)
    """)

    # One Keep/Tasks note can now produce several work items, so every work row
    # remembers which note it came from. Existing rows are one-to-one, so the
    # backfill is simply the old note_id.
    for table_name in ITEM_TABLES:
        _add_column(conn, table_name, "source_note_id TEXT")
        conn.execute(
            f"UPDATE {table_name} SET source_note_id = note_id "
            f"WHERE source_note_id IS NULL"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table_name}_source "
            f"ON {table_name} (source_note_id)"
        )

    # A cancellation that has to reach Google is not finished when the local
    # row flips. Keeping the outstanding delete in the database is what lets a
    # failed or interrupted delete be retried instead of being lost with the
    # process that attempted it.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_remote_deletes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            item_id TEXT NOT NULL,
            remote_id TEXT NOT NULL,
            token TEXT,
            requested_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            last_error TEXT,
            completed_at TEXT,
            UNIQUE(kind, item_id, remote_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pending_deletes_open
        ON pending_remote_deletes (completed_at, last_attempt_at)
    """)

    # The execution report is the only thing that tells the user what was
    # decided, and the undo button rides on it. Dropping it on a send failure
    # removes the safety net while the work stays done, so it is queued.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_note_id TEXT NOT NULL,
            entries TEXT NOT NULL,
            created_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            last_error TEXT,
            sent_at TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pending_reports_open
        ON pending_reports (sent_at, created_at)
    """)

    _add_column(conn, "persistent_reminders", "group_name TEXT")
    _add_column(conn, "web_monitors", "notified_at TEXT")

    now = utc_now()
    conn.execute("""
        UPDATE processed_notes
        SET status = 'processed'
        WHERE status IS NULL OR status NOT IN (
            'new', 'processing', 'waiting_downstream', 'processed', 'failed'
        )
    """)
    conn.execute(
        "UPDATE processed_notes SET updated_at = ? WHERE updated_at IS NULL",
        (now,),
    )

    # Preserve the old skip behavior after migration. Without this backfill,
    # every historical Keep note would be treated as edited on the first run.
    rows = conn.execute("""
        SELECT p.note_id, a.title, a.original_text
        FROM processed_notes AS p
        JOIN ai_results AS a ON a.note_id = p.note_id
        WHERE p.content_hash IS NULL
    """).fetchall()
    for note_id, title, original_text in rows:
        conn.execute(
            "UPDATE processed_notes SET content_hash = ? WHERE note_id = ?",
            (note_content_hash(title, original_text), note_id),
        )

    # Populate the new local work tables from classifications that already
    # existed before this migration. This does not call the LLM or the web.
    classified_rows = conn.execute("""
        SELECT note_id, title, original_text, intent, summary, actionable,
               calendar_ready, needs_target_resolution, needs_confirmation,
               missing_information, processed_at, scheduled_at
        FROM ai_results
        WHERE (intent = 'memo' AND NOT EXISTS (
                   SELECT 1 FROM memos WHERE memos.note_id = ai_results.note_id
               ))
           OR (intent = 'todo' AND NOT EXISTS (
                   SELECT 1 FROM todos WHERE todos.note_id = ai_results.note_id
               ))
           OR (intent = 'calendar' AND NOT EXISTS (
                   SELECT 1 FROM calendar_jobs
                   WHERE calendar_jobs.note_id = ai_results.note_id
               ))
           OR (intent = 'reminder' AND NOT EXISTS (
                   SELECT 1 FROM reminders
                   WHERE reminders.note_id = ai_results.note_id
               ))
    """).fetchall()
    for row in classified_rows:
        try:
            missing_information = json.loads(row[9] or "[]")
        except (TypeError, ValueError):
            missing_information = []
        save_structured_item(
            conn,
            row[0],
            row[1] or "",
            row[2] or "",
            {
                "intent": row[3],
                "summary": row[4] or "",
                "actionable": bool(row[5]),
                "calendar_ready": bool(row[6]),
                "needs_target_resolution": bool(row[7]),
                "needs_confirmation": bool(row[8]),
                "missing_information": missing_information,
                "scheduled_at": row[11],
            },
            now=row[10] or now,
        )

    for table_name in (
        "web_monitors",
        "reminders",
        "research_jobs",
        "calendar_jobs",
        "wake_jobs",
    ):
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table_name}_status "
            f"ON {table_name} (status)"
        )

    conn.commit()


def requeue_stale_running_jobs(
    conn,
    table_name,
    *,
    now=None,
    stale_after=timedelta(minutes=30),
):
    """Return interrupted jobs to retry without touching live recent work."""
    if table_name not in RECOVERABLE_JOB_TABLES:
        raise ValueError(f"unsupported recoverable job table: {table_name}")
    now = now or datetime.now().astimezone()
    cutoff = (now - stale_after).isoformat()
    cursor = conn.execute(f"""
        UPDATE {table_name}
        SET status = 'retry',
            last_error = COALESCE(last_error, '前回の処理中断を検出しました'),
            updated_at = ?
        WHERE status = 'running'
          AND (updated_at IS NULL OR updated_at < ?)
    """, (now.isoformat(), cutoff))
    conn.commit()
    return cursor.rowcount


def claim_note(conn, note_id, content_hash, updated_at=None):
    """Atomically claim new, edited, interrupted, or failed work."""
    updated_at = updated_at or utc_now()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT content_hash, status FROM processed_notes WHERE note_id = ?",
        (note_id,),
    ).fetchone()

    if row and row[0] == content_hash and row[1] in (
        "processed", "waiting_downstream"
    ):
        conn.commit()
        return False

    # A changed note produces a new generation. Undo buttons issued for the
    # previous generation refer to items that no longer exist at the same
    # position, so the revision is what lets undo refuse them.
    bump = 1 if (row is None or row[0] != content_hash) else 0
    conn.execute("""
        INSERT INTO processed_notes (
            note_id, content_hash, updated_at, status, last_error, revision
        ) VALUES (?, ?, ?, 'processing', NULL, 1)
        ON CONFLICT(note_id) DO UPDATE SET
            content_hash = excluded.content_hash,
            updated_at = excluded.updated_at,
            status = 'processing',
            last_error = NULL,
            revision = processed_notes.revision + ?
    """, (note_id, content_hash, updated_at, bump))
    conn.commit()
    return True


def note_revision(conn, note_id):
    row = conn.execute(
        "SELECT revision FROM processed_notes WHERE note_id = ?", (note_id,)
    ).fetchone()
    return row[0] if row else 0


def mark_note_processed(conn, note_id, content_hash, updated_at=None):
    conn.execute("""
        UPDATE processed_notes
        SET content_hash = ?, updated_at = ?, status = 'processed', last_error = NULL
        WHERE note_id = ?
    """, (content_hash, updated_at or utc_now(), note_id))


def mark_note_waiting_downstream(conn, note_id, content_hash, updated_at=None):
    conn.execute("""
        UPDATE processed_notes
        SET content_hash = ?, updated_at = ?, status = 'waiting_downstream',
            last_error = NULL
        WHERE note_id = ?
    """, (content_hash, updated_at or utc_now(), note_id))


def mark_note_failed(conn, note_id, content_hash, error, updated_at=None):
    conn.execute("""
        INSERT INTO processed_notes (
            note_id, content_hash, updated_at, status, last_error
        ) VALUES (?, ?, ?, 'failed', ?)
        ON CONFLICT(note_id) DO UPDATE SET
            content_hash = excluded.content_hash,
            updated_at = excluded.updated_at,
            status = 'failed',
            last_error = excluded.last_error
    """, (note_id, content_hash, updated_at or utc_now(), str(error)[:2000]))
    conn.commit()


INTENT_TABLES = {
    "todo": "todos",
    "calendar": "calendar_jobs",
    "memo": "memos",
    "reminder": "reminders",
}


def _stamp_source_note(cursor, table_name, item_id, source_note_id):
    cursor.execute(
        f"UPDATE {table_name} SET source_note_id = ? WHERE note_id = ?",
        (source_note_id, item_id),
    )


def save_structured_item(
    cursor, note_id, title, text, result, now=None, source_note_id=None
):
    """Upsert the current memo/todo/calendar projection for one work item."""
    now = now or utc_now()
    source_note_id = source_note_id or note_id
    intent = result["intent"]
    summary = result["summary"]

    if intent != "todo":
        cursor.execute(
            "UPDATE todos SET status = 'superseded', updated_at = ? "
            "WHERE note_id = ? AND status != 'superseded'",
            (now, note_id),
        )
    if intent != "calendar":
        cursor.execute(
            "UPDATE calendar_jobs SET status = 'superseded', updated_at = ? "
            "WHERE note_id = ? AND status != 'superseded'",
            (now, note_id),
        )
    if intent != "memo":
        cursor.execute(
            "UPDATE memos SET status = 'superseded', updated_at = ? "
            "WHERE note_id = ? AND status != 'superseded'",
            (now, note_id),
        )
    if intent != "reminder":
        cursor.execute(
            "UPDATE reminders SET status = 'superseded', updated_at = ? "
            "WHERE note_id = ? AND status IN ('pending', 'waiting_information')",
            (now, note_id),
        )
    if intent != "web_monitor":
        cursor.execute(
            "UPDATE web_monitors SET status = 'superseded' "
            "WHERE note_id = ? AND status = 'active'",
            (note_id,),
        )
    if intent != "persistent_reminder":
        cursor.execute(
            "UPDATE persistent_reminders SET status = 'superseded', updated_at = ? "
            "WHERE source_note_id = ? AND status = 'active'",
            (now, note_id),
        )
    if intent != "research":
        cursor.execute(
            "UPDATE research_jobs SET status = 'superseded', updated_at = ? "
            "WHERE source_note_id = ? "
            "AND status IN ('pending', 'retry', 'waiting_notify')",
            (now, note_id),
        )

    if intent == "todo":
        cursor.execute("""
            INSERT INTO todos (
                note_id, title, original_text, summary, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(note_id) DO UPDATE SET
                title = excluded.title,
                original_text = excluded.original_text,
                summary = excluded.summary,
                status = 'pending',
                updated_at = excluded.updated_at
        """, (note_id, title, text, summary, now, now))
    elif intent == "calendar":
        event_title = result.get("event_title") or summary
        event_start = result.get("event_start")
        event_end = result.get("event_end")
        all_day = int(bool(result.get("all_day")))
        event_location = result.get("event_location")
        event_description = result.get("event_description")
        calendar_status = (
            "pending"
            if result.get("calendar_ready") and event_title and event_start
            else "waiting_information"
        )
        cursor.execute("""
            INSERT INTO calendar_jobs (
                note_id, title, original_text, summary, calendar_ready,
                missing_information, status, created_at, updated_at,
                event_title, event_start, event_end, all_day,
                event_location, event_description, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(note_id) DO UPDATE SET
                title = excluded.title,
                original_text = excluded.original_text,
                summary = excluded.summary,
                calendar_ready = excluded.calendar_ready,
                missing_information = excluded.missing_information,
                event_title = excluded.event_title,
                event_start = excluded.event_start,
                event_end = excluded.event_end,
                all_day = excluded.all_day,
                event_location = excluded.event_location,
                event_description = excluded.event_description,
                status = CASE
                    WHEN calendar_jobs.status = 'created'
                     AND calendar_jobs.event_title = excluded.event_title
                     AND calendar_jobs.event_start = excluded.event_start
                     AND calendar_jobs.event_end IS excluded.event_end
                     AND calendar_jobs.all_day = excluded.all_day
                     AND calendar_jobs.event_location IS excluded.event_location
                     AND calendar_jobs.event_description IS excluded.event_description
                    THEN 'created'
                    ELSE excluded.status
                END,
                calendar_event_id = CASE
                    WHEN calendar_jobs.event_title = excluded.event_title
                     AND calendar_jobs.event_start = excluded.event_start
                     AND calendar_jobs.event_end IS excluded.event_end
                     AND calendar_jobs.all_day = excluded.all_day
                    THEN calendar_jobs.calendar_event_id
                    ELSE NULL
                END,
                last_error = NULL,
                updated_at = excluded.updated_at
        """, (
            note_id,
            title,
            text,
            summary,
            int(result["calendar_ready"]),
            json.dumps(result["missing_information"], ensure_ascii=False),
            calendar_status,
            now,
            now,
            event_title,
            event_start,
            event_end,
            all_day,
            event_location,
            event_description,
        ))
    elif intent == "memo":
        cursor.execute("""
            INSERT INTO memos (
                note_id, title, original_text, summary, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'active', ?, ?)
            ON CONFLICT(note_id) DO UPDATE SET
                title = excluded.title,
                original_text = excluded.original_text,
                summary = excluded.summary,
                status = 'active',
                updated_at = excluded.updated_at
        """, (note_id, title, text, summary, now, now))
    elif intent == "reminder":
        scheduled_at = result.get("scheduled_at")
        recurrence = result.get("recurrence")
        notification_text = result.get("notification_text") or summary
        status = "pending" if scheduled_at else "waiting_information"
        cursor.execute("""
            INSERT INTO reminders (
                note_id, title, original_text, summary, scheduled_at, recurrence, status,
                notified_at, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            ON CONFLICT(note_id) DO UPDATE SET
                title = excluded.title,
                original_text = excluded.original_text,
                summary = excluded.summary,
                scheduled_at = excluded.scheduled_at,
                recurrence = excluded.recurrence,
                status = CASE
                    WHEN reminders.status = 'notified'
                     AND reminders.scheduled_at = excluded.scheduled_at
                     AND reminders.summary = excluded.summary
                    THEN 'notified'
                    ELSE excluded.status
                END,
                notified_at = CASE
                    WHEN reminders.status = 'notified'
                     AND reminders.scheduled_at = excluded.scheduled_at
                     AND reminders.summary = excluded.summary
                    THEN reminders.notified_at
                    ELSE NULL
                END,
                last_error = NULL,
                updated_at = excluded.updated_at
        """, (
            note_id,
            title,
            text,
            notification_text,
            scheduled_at,
            recurrence,
            status,
            now,
            now,
        ))


    table_name = INTENT_TABLES.get(intent)
    if table_name:
        _stamp_source_note(cursor, table_name, note_id, source_note_id)


def is_generated_note(cursor, note_id):
    return cursor.execute(
        "SELECT 1 FROM generated_notes WHERE note_id = ?",
        (note_id,),
    ).fetchone() is not None


def upsert_wake_job(cursor, note_id, now=None, source_note_id=None):
    now = now or utc_now()
    source_note_id = source_note_id or source_note_id_of(note_id)
    cursor.execute("""
        INSERT INTO wake_jobs (note_id, status, created_at, updated_at)
        VALUES (?, 'pending', ?, ?)
        ON CONFLICT(note_id) DO UPDATE SET
            status = CASE
                WHEN wake_jobs.status = 'completed' THEN 'completed'
                ELSE 'pending'
            END,
            last_error = NULL,
            updated_at = excluded.updated_at
    """, (note_id, now, now))
    _stamp_source_note(cursor, "wake_jobs", note_id, source_note_id)


def upsert_research_job(cursor, source_note_id, plan, now=None):
    now = now or utc_now()
    requested_items = json.dumps(
        plan.get("requested_items") or [], ensure_ascii=False
    )
    cursor.execute("""
        INSERT INTO research_jobs (
            source_note_id, objective, query, requested_items, execute_at,
            save_to_keep, notify_mode, notify_at, notification_content_mode,
            status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        ON CONFLICT(source_note_id) DO UPDATE SET
            objective = excluded.objective,
            query = excluded.query,
            requested_items = excluded.requested_items,
            execute_at = excluded.execute_at,
            save_to_keep = excluded.save_to_keep,
            notify_mode = excluded.notify_mode,
            notify_at = excluded.notify_at,
            notification_content_mode = excluded.notification_content_mode,
            status = CASE
                WHEN research_jobs.objective = excluded.objective
                 AND research_jobs.requested_items = excluded.requested_items
                 AND research_jobs.save_to_keep = excluded.save_to_keep
                 AND research_jobs.notify_mode = excluded.notify_mode
                 AND research_jobs.notify_at IS excluded.notify_at
                 AND research_jobs.notification_content_mode =
                     excluded.notification_content_mode
                 AND research_jobs.status IN ('completed', 'waiting_notify')
                THEN research_jobs.status
                ELSE 'pending'
            END,
            result_text = CASE
                WHEN research_jobs.objective = excluded.objective
                 AND research_jobs.requested_items = excluded.requested_items
                THEN research_jobs.result_text ELSE NULL END,
            source_urls = CASE
                WHEN research_jobs.objective = excluded.objective
                 AND research_jobs.requested_items = excluded.requested_items
                THEN research_jobs.source_urls ELSE NULL END,
            last_error = NULL,
            updated_at = excluded.updated_at
    """, (
        source_note_id,
        plan["objective"],
        plan.get("query") or plan["objective"],
        requested_items,
        plan.get("execute_at"),
        int(bool(plan.get("save_to_keep"))),
        plan.get("notify_mode") or "none",
        plan.get("notify_at"),
        plan.get("notification_content_mode") or "result_summary",
        now,
        now,
    ))
    return cursor.execute(
        "SELECT id FROM research_jobs WHERE source_note_id = ?",
        (source_note_id,),
    ).fetchone()[0]


def upsert_web_monitor(cursor, note_id, summary, resolved, now=None, source_note_id=None):
    """Create a monitor or refresh its target after a Keep note edit."""
    now = now or utc_now()
    source_note_id = source_note_id or source_note_id_of(note_id)
    status = "found" if resolved["target_found"] else "active"
    monitor_urls = json.dumps(resolved["monitor_urls"], ensure_ascii=False)
    existing = cursor.execute(
        "SELECT id FROM web_monitors WHERE note_id = ? ORDER BY id LIMIT 1",
        (note_id,),
    ).fetchone()

    if existing:
        cursor.execute("""
            UPDATE web_monitors
            SET request_text = ?, search_query = ?, monitor_urls = ?,
                status = ?, last_checked_at = NULL, found_url = ?
            WHERE id = ?
        """, (
            summary,
            resolved["search_query"],
            monitor_urls,
            status,
            resolved["found_url"],
            existing[0],
        ))
        _stamp_source_note(cursor, "web_monitors", note_id, source_note_id)
        return False

    cursor.execute("""
        INSERT INTO web_monitors (
            note_id, request_text, search_query, monitor_urls,
            status, created_at, found_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        note_id,
        summary,
        resolved["search_query"],
        monitor_urls,
        status,
        now,
        resolved["found_url"],
    ))
    _stamp_source_note(cursor, "web_monitors", note_id, source_note_id)
    return True


def list_active_web_monitors(conn):
    """Return active web monitors as small records for commands and prompts."""
    return [
        {
            "id": row[0],
            "request_text": row[1],
            "search_query": row[2],
            "created_at": row[3],
        }
        for row in conn.execute("""
            SELECT id, request_text, search_query, created_at
            FROM web_monitors
            WHERE status = 'active'
            ORDER BY created_at, id
        """).fetchall()
    ]


def cancel_web_monitors(conn, target_ids):
    """Cancel only currently active monitors and return the cancelled records."""
    normalized_ids = sorted({int(value) for value in target_ids})
    if not normalized_ids:
        return []
    placeholders = ",".join("?" for _ in normalized_ids)
    rows = conn.execute(
        f"""
        SELECT id, request_text
        FROM web_monitors
        WHERE status = 'active' AND id IN ({placeholders})
        ORDER BY id
        """,
        normalized_ids,
    ).fetchall()
    if not rows:
        return []
    found_ids = [row[0] for row in rows]
    found_placeholders = ",".join("?" for _ in found_ids)
    conn.execute(
        f"""
        UPDATE web_monitors
        SET status = 'cancelled'
        WHERE status = 'active' AND id IN ({found_placeholders})
        """,
        found_ids,
    )
    return [{"id": row[0], "request_text": row[1]} for row in rows]


def list_pending_reminders(conn):
    """Return cancellable date/time reminders for prompts and user commands."""
    return [
        {
            "note_id": row[0],
            "summary": row[1],
            "scheduled_at": row[2],
            "recurrence": row[3],
            "created_at": row[4],
        }
        for row in conn.execute("""
            SELECT note_id, summary, scheduled_at, recurrence, created_at
            FROM reminders
            WHERE status = 'pending' AND scheduled_at IS NOT NULL
            ORDER BY scheduled_at, created_at, note_id
        """).fetchall()
    ]


def cancel_reminders(conn, target_note_ids):
    """Cancel pending reminders, including ones claimed for immediate sending."""
    normalized_ids = sorted({str(value) for value in target_note_ids if value})
    if not normalized_ids:
        return []
    placeholders = ",".join("?" for _ in normalized_ids)
    rows = conn.execute(
        f"""
        SELECT note_id, summary, scheduled_at, recurrence
        FROM reminders
        WHERE status IN ('pending', 'sending') AND note_id IN ({placeholders})
        ORDER BY scheduled_at, note_id
        """,
        normalized_ids,
    ).fetchall()
    if not rows:
        return []
    found_ids = [row[0] for row in rows]
    found_placeholders = ",".join("?" for _ in found_ids)
    conn.execute(
        f"""
        UPDATE reminders
        SET status = 'cancelled', last_error = NULL, updated_at = ?
        WHERE status IN ('pending', 'sending') AND note_id IN ({found_placeholders})
        """,
        [utc_now(), *found_ids],
    )
    return [
        {
            "note_id": row[0],
            "summary": row[1],
            "scheduled_at": row[2],
            "recurrence": row[3],
        }
        for row in rows
    ]


# =========================================================
# Multi-item notes
# =========================================================

def item_id_for(source_note_id, index):
    """Stable per-item key.

    Index 0 keeps the bare note id so every row written before multi-item
    support, and every downstream lookup that predates it, keeps working.
    """
    if index == 0:
        return source_note_id
    return f"{source_note_id}{ITEM_ID_SEPARATOR}{index}"


def source_note_id_of(item_id):
    return (item_id or "").split(ITEM_ID_SEPARATOR, 1)[0]


def supersede_orphan_items(cursor, source_note_id, active_item_ids, now=None):
    """Retire items that an edited note no longer produces."""
    now = now or utc_now()
    active = list(active_item_ids)
    placeholders = ",".join("?" for _ in active) or "NULL"
    for table_name in ("todos", "memos", "calendar_jobs", "wake_jobs"):
        cursor.execute(
            f"""
            UPDATE {table_name} SET status = 'superseded', updated_at = ?
            WHERE source_note_id = ? AND status != 'superseded'
              AND note_id NOT IN ({placeholders})
            """,
            [now, source_note_id, *active],
        )
    cursor.execute(
        f"""
        UPDATE reminders SET status = 'superseded', updated_at = ?
        WHERE source_note_id = ? AND status IN ('pending', 'waiting_information')
          AND note_id NOT IN ({placeholders})
        """,
        [now, source_note_id, *active],
    )
    cursor.execute(
        f"""
        UPDATE web_monitors SET status = 'superseded'
        WHERE source_note_id = ? AND status = 'active'
          AND note_id NOT IN ({placeholders})
        """,
        [source_note_id, *active],
    )
    # These two key their own rows by item id rather than carrying a
    # source_note_id column, so they are matched by the same prefix rule.
    like_prefix = f"{source_note_id}{ITEM_ID_SEPARATOR}%"
    cursor.execute(
        f"""
        UPDATE persistent_reminders
        SET status = 'superseded', updated_at = ?
        WHERE status = 'active'
          AND (source_note_id = ? OR source_note_id LIKE ?)
          AND source_note_id NOT IN ({placeholders})
        """,
        [now, source_note_id, like_prefix, *active],
    )
    cursor.execute(
        f"""
        UPDATE research_jobs
        SET status = 'superseded', updated_at = ?
        WHERE status IN ('pending', 'retry', 'waiting_notify')
          AND (source_note_id = ? OR source_note_id LIKE ?)
          AND source_note_id NOT IN ({placeholders})
        """,
        [now, source_note_id, like_prefix, *active],
    )


# research_jobs and persistent_reminders key their UNIQUE column by item id, so
# every table is matched by the same "item id belongs to this note" rule.
OUTSTANDING_WORK_QUERIES = (
    ("calendar_jobs", "note_id",
     ("pending", "retry", "running", "waiting_auth", "waiting_information")),
    ("wake_jobs", "note_id", ("pending", "retry", "running")),
    ("research_jobs", "source_note_id", ("pending", "retry", "running")),
)


def source_note_has_outstanding_work(conn, source_note_id, exclude_item_id=None):
    """True while any sibling item from the same note still needs a worker.

    A note that produced three items must not complete its Google Task until
    the last of them is done, otherwise the user sees the task disappear while
    part of the request is still queued.
    """
    like_prefix = f"{source_note_id}{ITEM_ID_SEPARATOR}%"
    for table_name, column, statuses in OUTSTANDING_WORK_QUERIES:
        placeholders = ",".join("?" for _ in statuses)
        sql = (
            f"SELECT {column} FROM {table_name} "
            f"WHERE ({column} = ? OR {column} LIKE ?) "
            f"AND status IN ({placeholders})"
        )
        params = [source_note_id, like_prefix]
        for (row_id,) in conn.execute(sql, [*params, *statuses]).fetchall():
            if exclude_item_id is not None and row_id == exclude_item_id:
                continue
            return True
    return False


def requeue_stale_found_monitors(conn, now=None, stale_after=timedelta(minutes=10)):
    """Re-arm monitors that were claimed as found but never delivered.

    monitor_worker claims 'found' before notifying so a cancelled monitor never
    produces a stale alert. That claim would otherwise swallow the discovery if
    the process died before the notification went out.
    """
    now = now or datetime.now().astimezone()
    cutoff = (now - stale_after).isoformat()
    cursor = conn.execute("""
        UPDATE web_monitors
        SET status = 'active', found_url = NULL
        WHERE status = 'found' AND notified_at IS NULL
          AND (last_checked_at IS NULL OR last_checked_at < ?)
    """, (cutoff,))
    conn.commit()
    return cursor.rowcount


# =========================================================
# Outstanding remote deletions
# =========================================================

def enqueue_remote_delete(conn, kind, item_id, remote_id, token=None, now=None):
    """Record that something still has to be removed from an external service."""
    conn.execute("""
        INSERT INTO pending_remote_deletes (
            kind, item_id, remote_id, token, requested_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(kind, item_id, remote_id) DO UPDATE SET
            completed_at = NULL,
            token = COALESCE(excluded.token, pending_remote_deletes.token)
    """, (kind, item_id, remote_id, token, now or utc_now()))


def open_remote_deletes(conn, kind=None, limit=20):
    sql = """
        SELECT id, kind, item_id, remote_id, attempts, last_error
        FROM pending_remote_deletes
        WHERE completed_at IS NULL
    """
    params = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY requested_at LIMIT ?"
    params.append(limit)
    return [
        {
            "id": row[0], "kind": row[1], "item_id": row[2],
            "remote_id": row[3], "attempts": row[4], "last_error": row[5],
        }
        for row in conn.execute(sql, params).fetchall()
    ]


def mark_remote_delete_done(conn, delete_id, now=None):
    conn.execute(
        "UPDATE pending_remote_deletes SET completed_at = ?, last_error = NULL "
        "WHERE id = ?",
        (now or utc_now(), delete_id),
    )
    conn.commit()


def mark_remote_delete_failed(conn, delete_id, error, now=None):
    timestamp = now or utc_now()
    conn.execute("""
        UPDATE pending_remote_deletes
        SET attempts = attempts + 1, last_attempt_at = ?, last_error = ?
        WHERE id = ?
    """, (timestamp, str(error)[:2000], delete_id))
    conn.commit()


def has_open_remote_delete(conn, kind, item_id):
    return conn.execute("""
        SELECT 1 FROM pending_remote_deletes
        WHERE kind = ? AND item_id = ? AND completed_at IS NULL
    """, (kind, item_id)).fetchone() is not None


def drain_remote_deletes(conn, kind, deleter, limit=20):
    """Retry outstanding deletions. Returns (done, failed)."""
    done = 0
    failed = 0
    for entry in open_remote_deletes(conn, kind, limit):
        try:
            deleter(entry["item_id"], entry["remote_id"])
        except Exception as error:
            mark_remote_delete_failed(conn, entry["id"], error)
            failed += 1
            continue
        mark_remote_delete_done(conn, entry["id"])
        done += 1
    return done, failed


# =========================================================
# Execution reports awaiting delivery
# =========================================================

def enqueue_report(conn, source_note_id, entries, now=None):
    cursor = conn.execute("""
        INSERT INTO pending_reports (source_note_id, entries, created_at)
        VALUES (?, ?, ?)
    """, (source_note_id, json.dumps(entries, ensure_ascii=False), now or utc_now()))
    return cursor.lastrowid


def open_reports(conn, limit=20):
    return [
        {"id": row[0], "source_note_id": row[1], "entries": json.loads(row[2]),
         "attempts": row[3]}
        for row in conn.execute("""
            SELECT id, source_note_id, entries, attempts
            FROM pending_reports
            WHERE sent_at IS NULL
            ORDER BY created_at
            LIMIT ?
        """, (limit,)).fetchall()
    ]


def mark_report_sent(conn, report_id, now=None):
    conn.execute(
        "UPDATE pending_reports SET sent_at = ?, last_error = NULL WHERE id = ?",
        (now or utc_now(), report_id),
    )
    conn.commit()


def mark_report_failed(conn, report_id, error, now=None):
    timestamp = now or utc_now()
    conn.execute("""
        UPDATE pending_reports
        SET attempts = attempts + 1, last_attempt_at = ?, last_error = ?
        WHERE id = ?
    """, (timestamp, str(error)[:2000], report_id))
    conn.commit()


def drain_reports(conn, sender, limit=20):
    """Deliver queued reports. Returns (sent, failed)."""
    sent = 0
    failed = 0
    for report in open_reports(conn, limit):
        try:
            delivered = sender(report["entries"])
        except Exception as error:
            mark_report_failed(conn, report["id"], error)
            failed += 1
            continue
        if delivered is False:
            # Nothing to send (an empty report). Retiring it keeps the queue
            # from retrying something that can never succeed.
            mark_report_sent(conn, report["id"])
            continue
        mark_report_sent(conn, report["id"])
        sent += 1
    return sent, failed
