import json
import os
import time
from dataclasses import dataclass
from urllib.parse import quote, unquote

import requests
from project_paths import CONFIG_DIR

TASK_ID_PREFIX = "tasks:"
WEBHOOK_CONFIG_FILE = os.environ.get(
    "GOOGLE_CALENDAR_WEBHOOK_CONFIG_FILE",
    str(CONFIG_DIR / "calendar_webhook.json"),
)


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

    def _post(self, payload):
        request = dict(payload)
        request["secret"] = self.credentials["secret"]
        response = None
        for attempt in range(3):
            response = self.session.post(
                self.credentials["endpoint_url"],
                json=request,
                timeout=30,
            )
            if response.status_code not in {404, 429, 500, 502, 503, 504}:
                break
            if attempt < 2:
                time.sleep(attempt + 1)
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(
                f"Google Tasks操作に失敗しました: {result.get('error', '不明なエラー')}"
            )
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

    def get(self, task_id):
        for item in self.all():
            if item.id == task_id:
                return item
        return None

    def sync(self):
        return None


def get_tasks_inbox_client():
    return TasksInboxClient()
