import json
import os
import secrets

import requests

import _bootstrap
from project_paths import CONFIG_DIR

from tasks_client import load_tasks_webhook_credentials


CONFIG_FILE = str(CONFIG_DIR / "tasks_event.json")


def create_signal_url():
    topic = "ai-inbox-" + secrets.token_urlsafe(32)
    return "https://ntfy.sh/" + topic


def configure():
    credentials = load_tasks_webhook_credentials()
    if not credentials:
        raise RuntimeError("Apps Scriptの接続設定がありません")

    signal_url = create_signal_url()
    response = requests.post(
        credentials["endpoint_url"],
        json={
            "action": "tasks_signal_configure",
            "secret": credentials["secret"],
            "signal_url": signal_url,
        },
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "GASの起動信号設定に失敗しました")

    with open(CONFIG_FILE, "w", encoding="utf-8") as file:
        json.dump({"signal_url": signal_url}, file, ensure_ascii=False, indent=2)
        file.write("\n")
    print("Google Tasksの起動信号を設定しました。")
    print("秘密のトピックURLは tasks_event.json に保存しました。")


if __name__ == "__main__":
    configure()
