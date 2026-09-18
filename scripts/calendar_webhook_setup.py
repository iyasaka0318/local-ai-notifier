import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse

import requests

import _bootstrap
from project_paths import CONFIG_DIR


CONFIG_FILE = CONFIG_DIR / "calendar_webhook.json"


def valid_web_app_url(value):
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "script.google.com"
        and parsed.path.startswith("/macros/s/")
        and parsed.path.endswith("/exec")
    )


def main():
    secret = secrets.token_urlsafe(48)
    print("Google Calendar Apps Script 設定")
    print()
    print("次の秘密キーをApps Scriptのスクリプトプロパティへ設定してください。")
    print("キー名: CALENDAR_WEBHOOK_SECRET")
    print("値:")
    print(secret)
    print()
    print("Apps Scriptをウェブアプリとしてデプロイした後、/execで終わるURLを入力します。")
    endpoint_url = input("ウェブアプリURL: ").strip()
    if not valid_web_app_url(endpoint_url):
        raise SystemExit("Apps Scriptの正しいウェブアプリURLではありません。")

    print("接続とカレンダー権限を確認しています...")
    response = requests.post(
        endpoint_url,
        json={"action": "ping", "secret": secret},
        timeout=30,
    )
    response.raise_for_status()
    try:
        result = response.json()
    except ValueError as error:
        raise SystemExit(
            "Apps ScriptからJSON応答を受信できませんでした。公開範囲を確認してください。"
        ) from error
    if not result.get("ok"):
        raise SystemExit(f"Apps Scriptの確認に失敗しました: {result.get('error', '不明なエラー')}")

    temporary_file = CONFIG_FILE.with_suffix(".json.tmp")
    temporary_file.write_text(
        json.dumps(
            {"endpoint_url": endpoint_url, "secret": secret},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(temporary_file, CONFIG_FILE)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass
    print("設定完了。以後はApps Script経由でカレンダーへ登録します。")


if __name__ == "__main__":
    main()
