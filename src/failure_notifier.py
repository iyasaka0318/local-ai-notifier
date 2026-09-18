import hashlib
import re
from datetime import datetime, timedelta

import requests

from state_store import utc_now


NTFY_URL = "https://ntfy.sh"
DEFAULT_COOLDOWN = timedelta(hours=6)


def _clean_error(error):
    raw = str(error or "不明なエラー")
    raw = re.sub(r"https?://\S+", "[URL省略]", raw)
    raw = re.sub(
        r"(?i)(token|password|secret|authorization|api[_-]?key)\s*[:=]\s*\S+",
        r"\1=[伏字]",
        raw,
    )
    return " ".join(raw.split())[:300] or "詳細情報なし"


def classify_error(error):
    """Return a short Japanese category suitable for a phone notification."""
    name = type(error).__name__.lower()
    message = str(error or "").lower()
    if "timeout" in name or "timed out" in message or "タイムアウト" in message:
        return "通信タイムアウト"
    if any(value in message for value in ("401", "403", "unauthorized", "forbidden")):
        return "認証・権限エラー"
    if any(value in name for value in ("credential", "auth")):
        return "認証・権限エラー"
    if "json" in name or "decode" in name or "json" in message:
        return "応答形式エラー"
    if "sqlite" in name or "database" in name or "database" in message:
        return "データベースエラー"
    if "http" in name or re.search(r"\b[45]\d\d\b", message):
        return "外部サービスのHTTPエラー"
    if any(value in name for value in ("connection", "connect")):
        return "接続エラー"
    if any(value in message for value in ("connection", "接続", "name resolution")):
        return "接続エラー"
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return "入力・データ形式エラー"
    return "処理エラー"


def _default_sender(topic, message, title):
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


def notify_processing_failure(
    conn,
    topic,
    component,
    item_id,
    error,
    *,
    retrying=True,
    sender=None,
    now=None,
    cooldown=DEFAULT_COOLDOWN,
):
    """Notify once per failure/cooldown and never raise on notification failure."""
    if not topic:
        return False
    now = now or datetime.now().astimezone()
    error_type = classify_error(error)
    summary = _clean_error(error)
    item_id = str(item_id or "unknown")
    fingerprint = hashlib.sha256(
        f"{component}\0{item_id}\0{error_type}\0{summary}".encode("utf-8")
    ).hexdigest()
    row = conn.execute(
        "SELECT last_notified_at FROM failure_notifications WHERE fingerprint = ?",
        (fingerprint,),
    ).fetchone()
    should_notify = True
    if row and row[0]:
        try:
            last_notified = datetime.fromisoformat(row[0])
            if last_notified.tzinfo is None:
                last_notified = last_notified.replace(tzinfo=now.tzinfo)
            should_notify = now - last_notified.astimezone(now.tzinfo) >= cooldown
        except (TypeError, ValueError):
            pass

    timestamp = now.isoformat()
    conn.execute("""
        INSERT INTO failure_notifications (
            fingerprint, component, item_id, error_type, error_summary,
            first_seen_at, last_seen_at, last_notified_at, occurrence_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(fingerprint) DO UPDATE SET
            last_seen_at = excluded.last_seen_at,
            occurrence_count = failure_notifications.occurrence_count + 1,
            last_notified_at = CASE
                WHEN excluded.last_notified_at IS NOT NULL
                THEN excluded.last_notified_at
                ELSE failure_notifications.last_notified_at
            END
    """, (
        fingerprint,
        component,
        item_id,
        error_type,
        summary,
        timestamp,
        timestamp,
        timestamp if should_notify else None,
    ))
    conn.commit()
    if not should_notify:
        return False

    retry_text = "自動で再試行します。" if retrying else "自動再試行は行いません。"
    message = (
        "ローカルAIの処理に失敗しました。\n"
        f"処理: {component}\n"
        f"種類: {error_type}\n"
        f"原因: {summary}\n"
        f"対応: {retry_text}"
    )
    try:
        (sender or _default_sender)(topic, message, "ローカルAI 処理失敗")
    except Exception as notify_error:
        # ntfy障害を通知しようとして無限再帰・大量送信しない。
        print(f"Failure notification could not be sent: {_clean_error(notify_error)}")
        return False
    return True
