"""One record per user-visible automation action.

This single table backs three features that would otherwise each need their own
bookkeeping:

* the execution report sent after every note (so the user never has to open an
  app to confirm that something landed),
* resolving vague spoken references such as "さっきのリマインダー",
* one-tap undo from the ntfy notification.

Rows are immutable except for the undo columns, so the log doubles as an audit
trail of what the classifier actually decided.
"""

import secrets

from state_store import (
    enqueue_remote_delete,
    has_open_remote_delete,
    utc_now,
)


# Kinds are also the notification grouping key, so keep them user-meaningful.
KIND_LABELS = {
    "reminder": "リマインダー",
    "persistent_reminder": "継続リマインド",
    "calendar": "カレンダー予定",
    "todo": "TODO",
    "memo": "メモ",
    "web_monitor": "Web監視",
    "research": "調査",
    "wake_briefing": "起床ブリーフィング",
}

UNDOABLE_KINDS = frozenset({
    "reminder",
    "persistent_reminder",
    "calendar",
    "todo",
    "memo",
    "web_monitor",
    "research",
})


def new_token():
    # Short enough to survive an ntfy action payload, wide enough to not collide.
    return secrets.token_urlsafe(9)


def record_action(
    conn,
    *,
    source_note_id,
    item_id,
    kind,
    summary,
    detail=None,
    fallback_reason=None,
    note_revision=0,
    target_key=None,
    now=None,
):
    """Append one action and return its undo token.

    ``note_revision`` pins the action to the generation of the note it came
    from, and ``target_key`` names the row the undo has to reach when that is
    not the item id itself (a deduplicated persistent reminder reuses an
    existing row, so the item id points at nothing).
    """
    token = new_token()
    conn.execute("""
        INSERT INTO action_log (
            token, source_note_id, item_id, kind, summary, detail,
            fallback_reason, created_at, note_revision, target_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        token,
        source_note_id,
        item_id,
        kind,
        summary,
        detail,
        fallback_reason,
        now or utc_now(),
        note_revision,
        target_key or item_id,
    ))
    return token


def recent_actions(conn, limit=12):
    """Newest first, so the classifier can resolve 'さっき' by position."""
    return [
        {
            "token": row[0],
            "item_id": row[1],
            "kind": row[2],
            "summary": row[3],
            "detail": row[4],
            "created_at": row[5],
            "undone": bool(row[6]),
        }
        for row in conn.execute("""
            SELECT token, item_id, kind, summary, detail, created_at, undone_at
            FROM action_log
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
        """, (limit,)).fetchall()
    ]


def get_action(conn, token):
    row = conn.execute("""
        SELECT token, source_note_id, item_id, kind, summary, detail, undone_at,
               note_revision, target_key
        FROM action_log WHERE token = ?
    """, (token,)).fetchone()
    if row is None:
        return None
    return {
        "token": row[0],
        "source_note_id": row[1],
        "item_id": row[2],
        "kind": row[3],
        "summary": row[4],
        "detail": row[5],
        "undone": bool(row[6]),
        "note_revision": row[7],
        "target_key": row[8] or row[2],
    }


def find_action_by_item(conn, item_id):
    row = conn.execute("""
        SELECT token FROM action_log
        WHERE item_id = ? AND undone_at IS NULL
        ORDER BY created_at DESC, rowid DESC LIMIT 1
    """, (item_id,)).fetchone()
    return row[0] if row else None


# --------------------------------------------------------------------------
# Undo
# --------------------------------------------------------------------------

def _undo_reminder(conn, item_id):
    cursor = conn.execute("""
        UPDATE reminders SET status = 'cancelled', last_error = NULL, updated_at = ?
        WHERE note_id = ? AND status IN ('pending', 'waiting_information', 'sending')
    """, (utc_now(), item_id))
    return cursor.rowcount > 0


def _undo_persistent_reminder(conn, item_id):
    cursor = conn.execute("""
        UPDATE persistent_reminders
        SET status = 'cancelled', updated_at = ?
        WHERE source_note_id = ? AND status = 'active'
    """, (utc_now(), item_id))
    return cursor.rowcount > 0


def _undo_todo(conn, item_id):
    cursor = conn.execute("""
        UPDATE todos SET status = 'cancelled', updated_at = ?
        WHERE note_id = ? AND status = 'pending'
    """, (utc_now(), item_id))
    return cursor.rowcount > 0


def _undo_memo(conn, item_id):
    cursor = conn.execute("""
        UPDATE memos SET status = 'cancelled', updated_at = ?
        WHERE note_id = ? AND status = 'active'
    """, (utc_now(), item_id))
    return cursor.rowcount > 0


def _undo_web_monitor(conn, item_id):
    cursor = conn.execute("""
        UPDATE web_monitors SET status = 'cancelled'
        WHERE note_id = ? AND status = 'active'
    """, (item_id,))
    return cursor.rowcount > 0


def _undo_research(conn, item_id):
    cursor = conn.execute("""
        UPDATE research_jobs SET status = 'cancelled', updated_at = ?
        WHERE source_note_id = ? AND status IN ('pending', 'retry', 'running')
    """, (utc_now(), item_id))
    return cursor.rowcount > 0


def _undo_calendar(conn, item_id, pending_calendar_deletes=None):
    row = conn.execute("""
        SELECT status, calendar_event_id FROM calendar_jobs WHERE note_id = ?
    """, (item_id,)).fetchone()
    if row is None:
        return False
    status, calendar_event_id = row
    already_cancelled = status == 'cancelled'
    # Cancel locally first so a worker cannot re-create the event while the
    # remote delete is in flight.
    conn.execute("""
        UPDATE calendar_jobs SET status = 'cancelled', last_error = NULL, updated_at = ?
        WHERE note_id = ?
    """, (utc_now(), item_id))
    if calendar_event_id:
        # The queue is what makes the deletion survive a failed request, a
        # closed process, or a second tap: an in-memory list would lose it.
        enqueue_remote_delete(conn, "calendar", item_id, calendar_event_id)
        if pending_calendar_deletes is not None:
            pending_calendar_deletes.append((item_id, calendar_event_id))
    return not already_cancelled


def run_pending_calendar_deletes(pending, deleter):
    """Best-effort immediate pass. The queue still owns the retry."""
    errors = []
    for item_id, event_id in pending or ():
        try:
            deleter(item_id, event_id)
        except Exception as error:
            errors.append(error)
    return errors


def undo_action(conn, token, pending_calendar_deletes=None, now=None):
    """Reverse one logged action. Returns (ok, message) for the user.

    The caller owns the transaction: this function never commits or rolls back,
    so it composes with a larger unit of work. Remote calendar deletions are
    collected into ``pending_calendar_deletes`` instead of being issued here, so
    no HTTP request is made while a write lock is held.
    """
    action = get_action(conn, token)
    if action is None:
        return False, "対象の操作が見つかりませんでした。"
    if action["undone"]:
        if action["kind"] == "calendar" and has_open_remote_delete(
            conn, "calendar", action["target_key"]
        ):
            # Locally cancelled, but Google still has the event. Saying
            # "already undone" here would strand it permanently.
            return True, (
                f"取り消し済みですが、カレンダーからの削除が未完了です。"
                f"再試行します: {action['summary']}"
            )
        return True, f"すでに取り消し済みです: {action['summary']}"

    # Item ids are positional within a note, so a re-classification can put a
    # different item at the same id. Undoing across that boundary would cancel
    # something the user never pointed at.
    current = conn.execute(
        "SELECT revision FROM processed_notes WHERE note_id = ?",
        (action["source_note_id"],),
    ).fetchone()
    if current is not None and current[0] != action["note_revision"]:
        return False, (
            f"このメモは編集されたため、この取り消しは使えません: {action['summary']}"
        )

    kind = action["kind"]
    item_id = action["target_key"]
    try:
        if kind == "reminder":
            changed = _undo_reminder(conn, item_id)
        elif kind == "persistent_reminder":
            changed = _undo_persistent_reminder(conn, item_id)
        elif kind == "calendar":
            changed = _undo_calendar(conn, item_id, pending_calendar_deletes)
        elif kind == "todo":
            changed = _undo_todo(conn, item_id)
        elif kind == "memo":
            changed = _undo_memo(conn, item_id)
        elif kind == "web_monitor":
            changed = _undo_web_monitor(conn, item_id)
        elif kind == "research":
            changed = _undo_research(conn, item_id)
        else:
            return False, f"この操作は取り消しに対応していません: {action['summary']}"
    except Exception as error:
        conn.execute(
            "UPDATE action_log SET undo_error = ? WHERE token = ?",
            (str(error)[:2000], token),
        )
        return False, f"取り消しに失敗しました: {action['summary']}"

    conn.execute(
        "UPDATE action_log SET undone_at = ?, undo_error = NULL WHERE token = ?",
        (now or utc_now(), token),
    )
    if not changed:
        # The row already moved on (fired, completed, superseded). Log it as
        # undone anyway so a second tap does not retry forever.
        return True, f"すでに実行済みのため取り消せませんでした: {action['summary']}"
    return True, f"取り消しました: {action['summary']}"
