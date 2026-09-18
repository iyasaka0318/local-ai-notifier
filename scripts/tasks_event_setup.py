import argparse
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


def existing_signal_url():
    if not os.path.exists(CONFIG_FILE):
        return None
    with open(CONFIG_FILE, encoding="utf-8") as file:
        return (json.load(file).get("signal_url") or "").strip() or None


def configure(reuse=False):
    credentials = load_tasks_webhook_credentials()
    if not credentials:
        raise RuntimeError("Apps Scriptの接続設定がありません")

    if reuse:
        # Re-apply the topic the listener is already subscribed to. Generating a
        # fresh one would silently orphan the running listener.
        signal_url = existing_signal_url()
        if not signal_url:
            raise RuntimeError(
                "再適用できる起動信号URLがありません。--reuse なしで実行してください"
            )
    else:
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

    if reuse:
        print("既存の起動信号URLをApps Scriptへ再適用しました。")
        print("リスナーの再起動は不要です。")
        return

    with open(CONFIG_FILE, "w", encoding="utf-8") as file:
        json.dump({"signal_url": signal_url}, file, ensure_ascii=False, indent=2)
        file.write("\n")
    print("Google Tasksの起動信号を設定しました。")
    print("秘密のトピックURLは tasks_event.json に保存しました。")
    print("リスナーを再起動してください:")
    print("  systemctl --user restart local-ai-notifier-listener.service")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Google Tasks起動信号の設定")
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="新しいトピックを作らず、tasks_event.json の既存URLを再適用します",
    )
    configure(reuse=parser.parse_args().reuse)
