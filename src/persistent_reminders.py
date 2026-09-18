import re
from difflib import SequenceMatcher

from state_store import utc_now


def normalize_task_text(text):
    text = (text or "").strip().lower()
    return re.sub(r"[\s　、。,.!！?？「」『』()（）]+", "", text)


def list_active_tasks(conn):
    return conn.execute("""
        SELECT id, task_text
        FROM persistent_reminders
        WHERE status = 'active'
        ORDER BY created_at, id
    """).fetchall()


def add_task(conn, source_note_id, task_text, now=None):
    now = now or utc_now()
    task_text = (task_text or "").strip()
    normalized = normalize_task_text(task_text)
    if not normalized:
        raise ValueError("リマインドする内容がありません")

    existing = conn.execute("""
        SELECT id FROM persistent_reminders
        WHERE status = 'active' AND normalized_text = ?
        ORDER BY id LIMIT 1
    """, (normalized,)).fetchone()
    if existing:
        return existing[0]

    conn.execute("""
        INSERT INTO persistent_reminders (
            source_note_id, task_text, normalized_text, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, 'active', ?, ?)
        ON CONFLICT(source_note_id) DO UPDATE SET
            task_text = excluded.task_text,
            normalized_text = excluded.normalized_text,
            status = 'active',
            last_notified_slot = NULL,
            last_error = NULL,
            completed_at = NULL,
            completed_by_note_id = NULL,
            updated_at = excluded.updated_at
    """, (source_note_id, task_text, normalized, now, now))
    return conn.execute(
        "SELECT id FROM persistent_reminders WHERE source_note_id = ?",
        (source_note_id,),
    ).fetchone()[0]


def _resolve_task_id(conn, target_task_id=None, task_text=None):
    active = list_active_tasks(conn)
    if target_task_id is not None:
        for task_id, _text in active:
            if task_id == target_task_id:
                return task_id

    target = normalize_task_text(task_text)
    if not target:
        return None

    exact = [task_id for task_id, text in active if normalize_task_text(text) == target]
    if len(exact) == 1:
        return exact[0]

    contained = [
        task_id for task_id, text in active
        if target in normalize_task_text(text) or normalize_task_text(text) in target
    ]
    if len(contained) == 1:
        return contained[0]

    scored = sorted(
        (
            SequenceMatcher(None, target, normalize_task_text(text)).ratio(),
            task_id,
        )
        for task_id, text in active
    )
    if not scored:
        return None
    best_score, best_id = scored[-1]
    second_score = scored[-2][0] if len(scored) > 1 else 0.0
    if best_score >= 0.72 and best_score - second_score >= 0.12:
        return best_id
    return None


def complete_task(
    conn,
    command_note_id,
    target_task_id=None,
    task_text=None,
    now=None,
):
    now = now or utc_now()
    previous = conn.execute("""
        SELECT target_task_id, status
        FROM persistent_reminder_commands
        WHERE note_id = ? AND action = 'complete'
    """, (command_note_id,)).fetchone()
    if previous and previous[1] == "completed":
        return previous[0]

    resolved_id = _resolve_task_id(conn, target_task_id, task_text)
    if resolved_id is None:
        raise ValueError("完了にするリマインドタスクを特定できませんでした")

    conn.execute("""
        UPDATE persistent_reminders
        SET status = 'completed', completed_at = ?, completed_by_note_id = ?,
            last_error = NULL, updated_at = ?
        WHERE id = ? AND status = 'active'
    """, (now, command_note_id, now, resolved_id))
    conn.execute("""
        INSERT INTO persistent_reminder_commands (
            note_id, action, target_task_id, status, created_at, updated_at
        ) VALUES (?, 'complete', ?, 'completed', ?, ?)
        ON CONFLICT(note_id) DO UPDATE SET
            target_task_id = excluded.target_task_id,
            status = 'completed',
            last_error = NULL,
            updated_at = excluded.updated_at
    """, (command_note_id, resolved_id, now, now))
    return resolved_id


def create_notify_now_command(conn, command_note_id, now=None):
    now = now or utc_now()
    conn.execute("""
        INSERT INTO persistent_reminder_commands (
            note_id, action, status, created_at, updated_at
        ) VALUES (?, 'notify_now', 'pending', ?, ?)
        ON CONFLICT(note_id) DO NOTHING
    """, (command_note_id, now, now))
