import getpass
import json
import os
from pathlib import Path
from uuid import getnode

import gkeepapi
import gpsoauth

import _bootstrap
from project_paths import CONFIG_DIR


CREDENTIAL_FILE = CONFIG_DIR / "keep_credentials.json"
DEFAULT_EMAIL = os.environ.get("KEEP_DEFAULT_EMAIL", "")


def main() -> None:
    print("Google Keep 再認証")
    print("認証情報はこのPC内だけで処理され、画面には表示しません。")
    print()

    prompt = (
        f"Googleアカウント [{DEFAULT_EMAIL}]: "
        if DEFAULT_EMAIL
        else "Googleアカウント: "
    )
    email = input(prompt).strip() or DEFAULT_EMAIL
    if not email:
        raise SystemExit("Googleアカウントが入力されていません。")
    web_token = getpass.getpass("EmbeddedSetup の oauth_token を貼り付けて Enter: ").strip()
    if not web_token:
        raise SystemExit("oauth_token が入力されていません。")

    android_id = f"{getnode():x}"
    print("一時トークンをマスタートークンへ交換しています...")
    response = gpsoauth.exchange_token(email, web_token, android_id)
    master_token = response.get("Token")
    if not master_token:
        error = response.get("Error", "不明な認証エラー")
        raise SystemExit(f"マスタートークンを取得できませんでした: {error}")

    print("Google Keepへの接続を確認しています...")
    keep = gkeepapi.Keep()
    keep.authenticate(email, master_token, device_id=android_id)

    temporary_file = CREDENTIAL_FILE.with_suffix(".json.tmp")
    temporary_file.write_text(
        json.dumps(
            {"email": email, "master_token": master_token},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(temporary_file, CREDENTIAL_FILE)

    print("認証成功。Keepの認証情報をローカルに保存しました。")
    print("以後は通常、自動処理のたびにログインする必要はありません。")


if __name__ == "__main__":
    main()
