import ipaddress
import json
import os
import re
import socket
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from charset_normalizer import from_bytes
from ddgs import DDGS

from failure_notifier import notify_processing_failure
from automation_config import (
    MAX_PAGE_BYTES,
    MAX_PAGE_TEXT_CHARS,
    MAX_RESEARCH_PAGES,
    MAX_RESEARCH_RESULTS,
    OLLAMA_THINK,
)
from instance_lock import SingleInstanceLock
from keep_client import get_authenticated_keep
from tasks_client import get_tasks_inbox_client
from output_policy import needs_japanese_rewrite
from project_paths import DB_PATH, RUNTIME_DIR, ensure_runtime_directories
from reminder_worker import parse_scheduled_at, send_notification
from state_store import (
    ensure_schema,
    mark_note_processed,
    requeue_stale_running_jobs,
    utc_now,
)


OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3:14b"
LOCAL_TIMEZONE = ZoneInfo("Asia/Tokyo")


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth:
            text = " ".join(data.split())
            if text:
                self.parts.append(text)

    def text(self):
        return "\n".join(self.parts)


def ask_ollama(system_prompt, user_data, schema):
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_data, ensure_ascii=False)},
            ],
            "format": schema,
            "keep_alive": "30m",
            "think": OLLAMA_THINK,
            "stream": False,
            "options": {"temperature": 0.1},
        },
        timeout=180,
    )
    response.raise_for_status()
    return json.loads(response.json()["message"]["content"])


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "result_text": {"type": "string"},
        "memo_title": {"type": "string"},
        "memo_text": {"type": "string"},
        "notification_title": {"type": "string"},
        "completion_text": {"type": "string"},
        "notification_summary": {"type": "string"},
        "notification_detailed": {"type": "string"},
        "unresolved_items": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "result_text", "memo_title", "memo_text", "notification_title",
        "completion_text", "notification_summary", "notification_detailed",
        "unresolved_items",
    ],
}


SUMMARY_PROMPT = """
You are a careful research assistant for a Japanese user. Every user-facing prose
field MUST be written in natural Japanese. English-only prose is forbidden. Keep
official proper nouns, product names, and URLs in their original form when needed.
ユーザー向けの文章は必ず日本語で生成してください。

Use only the supplied search results and fetched page text. Prioritize official
primary sources. Never invent, infer, or silently change facts. For every requested
item, give the answer or explicitly state in Japanese that it could not be confirmed.
Preserve disagreements between sources and place supporting URLs near key claims.

Create separate outputs for separate media:
- result_text: detailed, formal Japanese research result.
- memo_title: short Japanese title without the [AI結果] prefix.
- memo_text: readable Japanese Google Keep memo. Organize with headings such as
  "■ 開催場所" and bullet points where useful. Do not paste English source text.
- notification_title: short Japanese notification title.
- completion_text: one short Japanese sentence saying only that research finished.
- notification_summary: a few lines containing only the most important findings.
- notification_detailed: more detail than the summary but still suitable for ntfy;
  keep it under about 1,800 Japanese characters.

Do not reuse the same long text for every field. All explanations must be Japanese.
"""


JAPANESE_REWRITE_PROMPT = """
Rewrite the supplied research output for a Japanese user. Every prose field must be
natural Japanese. Preserve all facts, uncertainty, numbers, dates, proper nouns,
product names, and URLs exactly. Do not add facts. Do not translate URLs. Keep the
same JSON structure and the separate purposes and lengths of result, Keep memo,
completion notice, short notification, and detailed notification.
ユーザー向けの文章は必ず日本語で生成してください。
"""


USER_OUTPUT_FIELDS = (
    "result_text",
    "memo_title",
    "memo_text",
    "notification_title",
    "completion_text",
    "notification_summary",
    "notification_detailed",
)


def ensure_japanese_research_output(result):
    if any(needs_japanese_rewrite(result.get(field)) for field in USER_OUTPUT_FIELDS):
        result = ask_ollama(JAPANESE_REWRITE_PROMPT, result, SUMMARY_SCHEMA)
    english_fields = [
        field for field in USER_OUTPUT_FIELDS
        if needs_japanese_rewrite(result.get(field))
    ]
    if english_fields:
        raise RuntimeError(
            "調査結果を日本語に整形できませんでした: "
            + ", ".join(english_fields)
        )
    for field in USER_OUTPUT_FIELDS:
        if not (result.get(field) or "").strip():
            raise RuntimeError(f"調査結果の{field}が空です")
        result[field] = result[field].strip()
    result["notification_summary"] = result["notification_summary"][:700]
    result["notification_detailed"] = result["notification_detailed"][:1800]
    return result


def build_queries(objective, requested_items, base_query):
    # Search the official-source form first so page-body fetches are spent on the
    # strongest candidates before general articles and aggregators.
    queries = [f"{objective} 公式", base_query or objective]
    for item in requested_items[:3]:
        queries.append(f"{objective} {item} 公式")
    unique = []
    for query in queries:
        query = query.strip()
        if query and query not in unique:
            unique.append(query)
    return unique[:5]


def search_web(queries):
    def run_query(query):
        try:
            return query, list(DDGS().text(query, max_results=8))
        except Exception:
            return query, []

    by_url = {}
    if not queries:
        return []
    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as executor:
        query_results = executor.map(run_query, queries)
        for query, results in query_results:
            for result in results:
                url = (result.get("href") or "").strip()
                if not url or url in by_url:
                    continue
                by_url[url] = {
                    "title": result.get("title") or "",
                    "url": url,
                    "description": result.get("body") or "",
                    "query": query,
                }
                if len(by_url) >= MAX_RESEARCH_RESULTS:
                    return list(by_url.values())
    return list(by_url.values())


def validate_public_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("unsupported URL")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".local"):
        raise ValueError("local URL is not allowed")
    for info in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ValueError("non-public URL is not allowed")


def decode_response_body(response, raw):
    content_type = response.headers.get("content-type", "").lower()
    if "charset=" in content_type and response.encoding:
        encoding = response.encoding
    else:
        match = from_bytes(raw).best()
        encoding = match.encoding if match is not None else "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def fetch_page_text(url):
    current = url
    for _ in range(4):
        validate_public_url(current)
        response = requests.get(
            current,
            headers={"User-Agent": "LocalAIResearch/1.0"},
            timeout=20,
            allow_redirects=False,
            stream=True,
        )
        if 300 <= response.status_code < 400 and response.headers.get("location"):
            current = urljoin(current, response.headers["location"])
            continue
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "text/html" not in content_type and "text/plain" not in content_type:
            raise ValueError("unsupported content type")
        chunks = []
        size = 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_PAGE_BYTES:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        decoded = decode_response_body(response, raw)
        if "text/html" in content_type:
            parser = TextExtractor()
            parser.feed(decoded)
            decoded = parser.text()
        return current, decoded[:MAX_PAGE_TEXT_CHARS]
    raise ValueError("too many redirects")


def enrich_candidates(candidates):
    enriched = [dict(item) for item in candidates]
    fetched = 0
    for offset in range(0, len(enriched), MAX_RESEARCH_PAGES):
        if fetched >= MAX_RESEARCH_PAGES:
            break
        batch = enriched[offset:offset + MAX_RESEARCH_PAGES]
        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = [executor.submit(fetch_page_text, item["url"]) for item in batch]
            for item, future in zip(batch, futures):
                if fetched >= MAX_RESEARCH_PAGES:
                    future.cancel()
                    continue
                try:
                    final_url, page_text = future.result()
                except Exception:
                    continue
                item["url"] = final_url
                item["page_text"] = page_text
                fetched += 1
    return enriched


def run_research(objective, requested_items, query, save_to_keep=False):
    queries = build_queries(objective, requested_items, query)
    candidates = search_web(queries)
    if not candidates:
        raise RuntimeError("Web検索結果がありません")
    candidates = enrich_candidates(candidates)
    result = ask_ollama(
        SUMMARY_PROMPT,
        {
            "objective": objective,
            "requested_items": requested_items,
            "sources": candidates,
            "result_will_be_saved_to_keep": bool(save_to_keep),
        },
        SUMMARY_SCHEMA,
    )
    result = ensure_japanese_research_output(result)
    return result, [item["url"] for item in candidates]


def get_keep():
    return get_authenticated_keep()


def save_result_to_keep(conn, keep, job_id, memo_title, memo_text):
    source_task_id = str(job_id)
    existing = conn.execute("""
        SELECT note_id, status FROM generated_notes
        WHERE source_task_id = ? AND source_task_type = 'research'
    """, (source_task_id,)).fetchone()
    if existing:
        existing_note = keep.get(existing[0])
        if existing_note is not None:
            conn.execute(
                "UPDATE generated_notes SET status = 'created' WHERE note_id = ?",
                (existing[0],),
            )
            conn.execute(
                "UPDATE research_jobs SET generated_note_id = ? WHERE id = ?",
                (existing[0], job_id),
            )
            conn.commit()
            return existing[0]

    memo_title = re.sub(r"^\s*[\[［]AI結果[\]］]\s*", "", memo_title).strip()
    note = keep.createNote(f"[AI結果] {memo_title[:60]}", memo_text)
    timestamp = utc_now()
    conn.execute("""
        INSERT INTO generated_notes (
            note_id, source_task_id, source_task_type, status, created_at
        ) VALUES (?, ?, 'research', 'creating', ?)
        ON CONFLICT(source_task_id, source_task_type) DO UPDATE SET
            note_id = excluded.note_id,
            status = 'creating',
            created_at = excluded.created_at
    """, (note.id, source_task_id, timestamp))
    conn.commit()
    keep.sync()
    conn.execute(
        "UPDATE generated_notes SET status = 'created' WHERE note_id = ?",
        (note.id,),
    )
    conn.execute(
        "UPDATE research_jobs SET generated_note_id = ? WHERE id = ?",
        (note.id, job_id),
    )
    conn.commit()
    return note.id


def schedule_result_notification(
    conn, job_id, notification_title, notification_text, notify_at
):
    note_id = f"research:{job_id}:notify"
    timestamp = utc_now()
    conn.execute("""
        INSERT INTO reminders (
            note_id, title, original_text, summary, scheduled_at, recurrence,
            status, source_type, source_id, created_at, updated_at
        ) VALUES (?, ?, '', ?, ?, NULL, 'pending',
                  'research', ?, ?, ?)
        ON CONFLICT(note_id) DO UPDATE SET
            title = excluded.title,
            summary = excluded.summary,
            scheduled_at = excluded.scheduled_at,
            status = CASE WHEN reminders.status = 'notified'
                          THEN 'notified' ELSE 'pending' END,
            updated_at = excluded.updated_at
    """, (
        note_id, notification_title, notification_text, notify_at,
        str(job_id), timestamp, timestamp,
    ))
    conn.commit()


def select_notification_text(
    notification_content_mode,
    completion_text,
    notification_summary,
    notification_detailed_text,
    save_to_keep,
):
    if notification_content_mode == "completion_only":
        text = completion_text
    elif notification_content_mode == "detailed_result":
        text = notification_detailed_text
    else:
        text = notification_summary
    if save_to_keep and "Keep" not in text:
        text = f"{text.rstrip()}\n\n詳細はGoogle Keepに保存しました。"
    return text


def finish_source_note(
    conn,
    keep,
    source_note_id,
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
        if keep is None:
            keep = get_keep()
        note = keep.get(source_note_id)
        if note is not None and not note.trashed:
            note.trash()
            keep.sync()
    elif automation_source == "google_tasks":
        tasks_factory().complete_task(source_note_id)
    mark_note_processed(conn, source_note_id, content_hash)
    conn.commit()


def process_job(conn, job, topic):
    (
        job_id, source_note_id, objective, query, requested_items_json,
        execute_at, save_to_keep, notify_mode, notify_at, result_text,
        memo_title, memo_text, notification_title, completion_text, notification_text,
        notification_detailed_text, notification_content_mode,
        generated_note_id, notified_at,
    ) = job
    if execute_at:
        due_at = parse_scheduled_at(execute_at)
        if due_at > datetime.now(due_at.tzinfo):
            return False

    timestamp = utc_now()
    conn.execute("""
        UPDATE research_jobs
        SET status = 'running', started_at = COALESCE(started_at, ?),
            updated_at = ?, attempt_count = attempt_count + 1, last_error = NULL
        WHERE id = ?
    """, (timestamp, timestamp, job_id))
    conn.commit()

    requested_items = json.loads(requested_items_json or "[]")
    if not all((
        result_text, memo_title, memo_text, notification_title,
        completion_text, notification_text, notification_detailed_text,
    )):
        output, source_urls = run_research(
            objective, requested_items, query, bool(save_to_keep)
        )
        result_text = output["result_text"]
        memo_title = output["memo_title"]
        memo_text = output["memo_text"]
        notification_title = output["notification_title"]
        completion_text = output["completion_text"]
        notification_text = output["notification_summary"]
        notification_detailed_text = output["notification_detailed"]
        conn.execute("""
            UPDATE research_jobs
            SET result_text = ?, memo_title = ?, memo_text = ?,
                notification_title = ?, completion_text = ?, notification_text = ?,
                notification_detailed_text = ?, source_urls = ?, updated_at = ?
            WHERE id = ?
        """, (
            result_text, memo_title, memo_text, notification_title,
            completion_text, notification_text,
            notification_detailed_text,
            json.dumps(source_urls, ensure_ascii=False),
            utc_now(),
            job_id,
        ))
        conn.commit()
    outgoing_notification = select_notification_text(
        notification_content_mode,
        completion_text,
        notification_text,
        notification_detailed_text,
        bool(save_to_keep),
    )

    keep = None
    if save_to_keep and not generated_note_id:
        keep = get_keep()
        save_result_to_keep(conn, keep, job_id, memo_title, memo_text)

    if notify_mode == "after_completion" and not notified_at:
        send_notification(topic, outgoing_notification, notification_title)
        notified_at = utc_now()
        conn.execute(
            "UPDATE research_jobs SET notified_at = ?, updated_at = ? WHERE id = ?",
            (notified_at, notified_at, job_id),
        )
        conn.commit()
    elif notify_mode == "at_time" and notify_at:
        schedule_result_notification(
            conn, job_id, notification_title, outgoing_notification, notify_at
        )

    finish_source_note(conn, keep, source_note_id)
    completed_at = utc_now()
    conn.execute("""
        UPDATE research_jobs
        SET status = 'completed', completed_at = ?, updated_at = ?, last_error = NULL
        WHERE id = ?
    """, (completed_at, completed_at, job_id))
    conn.commit()
    return True


def main():
    ensure_runtime_directories()
    lock = SingleInstanceLock(
        str(RUNTIME_DIR / "research_worker.lock")
    )
    if not lock.acquire():
        return
    topic = os.environ["NTFY_TOPIC"]
    conn = sqlite3.connect(DB_PATH, timeout=30)
    ensure_schema(conn)
    requeue_stale_running_jobs(conn, "research_jobs")
    jobs = conn.execute("""
        SELECT id, source_note_id, objective, query, requested_items, execute_at,
               save_to_keep, notify_mode, notify_at, result_text,
               memo_title, memo_text, notification_title, completion_text,
               notification_text,
               notification_detailed_text, notification_content_mode,
               generated_note_id, notified_at
        FROM research_jobs
        WHERE status IN ('pending', 'retry')
        ORDER BY created_at
    """).fetchall()
    for job in jobs:
        try:
            process_job(conn, job, topic)
        except Exception as error:
            conn.execute("""
                UPDATE research_jobs
                SET status = 'retry', last_error = ?, updated_at = ?
                WHERE id = ?
            """, (str(error)[:2000], utc_now(), job[0]))
            conn.commit()
            notify_processing_failure(
                conn,
                topic,
                "調査処理",
                job[0],
                error,
                retrying=True,
            )
    conn.close()


if __name__ == "__main__":
    main()
