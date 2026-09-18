import json
import os
import time
from dataclasses import dataclass
from urllib.parse import quote, unquote

import requests
from project_paths import CONFIG_DIR

class RetryableWebhookError(RuntimeError):
    """The request did not land, but sending it again is safe."""


TASK_ID_PREFIX = "tasks:"
INGEST_TIMEOUT_SECONDS = 60
SIGNAL_TIMEOUT_SECONDS = 15
TASKS_EVENT_CONFIG_FILE = os.environ.get(
    "AI_TASKS_EVENT_CONFIG_FILE",
    str(CONFIG_DIR / "tasks_event.json"),
)
WEBHOOK_CONFIG_FILE = os.environ.get(
    "GOOGLE_CALENDAR_WEBHOOK_CONFIG_FILE",
    str(CONFIG_DIR / "calendar_webhook.json"),
)


def load_signal_url():
    """Topic the local listener is subscribed to, or None when unconfigured."""
    if not os.path.exists(TASKS_EVENT_CONFIG_FILE):
        return None
    with open(TASKS_EVENT_CONFIG_FILE, encoding="utf-8") as file:
        return (json.load(file).get("signal_url") or "").strip() or None


def send_tasks_signal(session=requests, signal_url=None):
    """Wake the local listener directly.

    Apps Script is deliberately not used for this. Google's outbound path to
    ntfy.sh intermittently hangs for tens of seconds, and that wait lands on
    whoever called the webhook. Both the phone and this machine can reach ntfy
    in well under a second, so the signal belongs on the caller's side.
    """
    signal_url = signal_url or load_signal_url()
    if not signal_url:
        return False
    response = session.post(
        signal_url,
        data="tasks_changed".encode("utf-8"),
        headers={"Title": "AI Inbox signal", "Priority": "3"},
        timeout=SIGNAL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return True


def load_tasks_webhook_credentials():
    endpoint_url = os.environ.get("GOOGLE_CALENDAR_WEBHOOK_URL")
    secret = os.environ.get("GOOGLE_CALENDAR_WEBHOOK_SECRET")
    if endpoint_url and secret:
        return {"endpoint_url": endpoint_url, "secret": secret}
    if not os.path.exists(WEBHOOK_CONFIG_FILE):
        return None
    with open(WEBHOOK_CONFIG_FILE, encoding="utf-8") as file:
        credentials = json.load(file)
    if not credentials.get("endpoint_url") or not credentials.get("secret"):
        raise RuntimeError("Apps Script設定に必要な値がありません")
    return credentials


@dataclass
class TaskInboxItem:
    client: "TasksInboxClient"
    remote_task_id: str
    task_list_id: str
    task_list_title: str
    is_dedicated_inbox: bool
    title: str
    text: str
    trashed: bool = False

    @property
    def id(self):
        return (
            TASK_ID_PREFIX
            + quote(self.task_list_id, safe="")
            + ":"
            + quote(self.remote_task_id, safe="")
        )

    def trash(self):
        self.client.complete_task(self.id)
        self.trashed = True


class TasksInboxClient:
    def __init__(self, credentials=None, session=requests):
        self.credentials = credentials or load_tasks_webhook_credentials()
        if not self.credentials:
            raise RuntimeError("Apps Scriptの設定がありません")
        self.session = session

    def _post(self, payload, timeout=30):
        request = dict(payload)
        request["secret"] = self.credentials["secret"]
        response = None
        for attempt in range(3):
            response = self.session.post(
                self.credentials["endpoint_url"],
                json=request,
                timeout=timeout,
            )
            if response.status_code not in {404, 429, 500, 502, 503, 504}:
                break
            if attempt < 2:
                time.sleep(attempt + 1)
        response.raise_for_status()

        content_type = (response.headers.get("content-type") or "").lower()
        if "json" not in content_type:
            # Apps Script answers an internal failure or a frontend cutoff with
            # an HTML error page under HTTP 200. Reporting the decode error
            # hides that, so name what actually came back.
            raise RuntimeError(
                "Apps Scriptがエラーページを返しました"
                f"（content-type: {content_type or '不明'}）。"
                "実行時間の超過かロック競合の可能性があります"
            )
        result = response.json()
        if not result.get("ok"):
            error = result.get("error", "不明なエラー")
            if result.get("retryable"):
                raise RetryableWebhookError(f"一時的な失敗です: {error}")
            raise RuntimeError(f"Google Tasks操作に失敗しました: {error}")
        return result

    def all(self):
        result = self._post({"action": "tasks_list"})
        return [
            TaskInboxItem(
                client=self,
                remote_task_id=str(item["id"]),
                task_list_id=str(item.get("task_list_id") or ""),
                task_list_title=item.get("task_list_title") or "",
                is_dedicated_inbox=bool(item.get("dedicated_inbox")),
                title=item.get("title") or "",
                text=item.get("notes") or "",
            )
            for item in result.get("tasks") or []
        ]

    def complete_task(self, task_id):
        encoded_id = str(task_id)
        if encoded_id.startswith(TASK_ID_PREFIX):
            encoded_id = encoded_id[len(TASK_ID_PREFIX):]
        if ":" in encoded_id:
            encoded_list_id, encoded_task_id = encoded_id.split(":", 1)
            list_id = unquote(encoded_list_id)
            remote_id = unquote(encoded_task_id)
        else:
            # Backward compatibility for IDs made before multi-list support.
            list_id = ""
            remote_id = unquote(encoded_id)
        payload = {"action": "tasks_complete", "task_id": remote_id}
        if list_id:
            payload["task_list_id"] = list_id
        self._post(payload)

    def ingest_text(self, text, title=None, request_id=None):
        """Push one spoken memo straight into the AI Inbox.

        This is the path a phone automation app uses instead of going through
        an assistant that may reword the utterance. ``request_id`` makes a
        retry safe: the same id inside the dedupe window returns the original
        task rather than creating a second one.
        """
        payload = {"action": "tasks_ingest", "text": text}
        if title:
            payload["title"] = title
        if request_id:
            payload["request_id"] = request_id
        # Apps Script can take longer than a plain read under trigger
        # contention, and a client-side timeout here is worse than waiting:
        # the task is created either way, so giving up early only hides a
        # success and invites a duplicate on retry.
        result = self._post(payload, timeout=INGEST_TIMEOUT_SECONDS)

        # The task is stored, so a signal failure only costs latency: the
        # periodic poll still picks it up.
        try:
            result["signalled"] = send_tasks_signal(self.session)
        except Exception as error:
            result["signalled"] = False
            result["signal_error"] = str(error)[:200]
        return result

    def get(self, task_id):
        for item in self.all():
            if item.id == task_id:
                return item
        return None

    def sync(self):
        return None


def get_tasks_inbox_client():
    return TasksInboxClient()
