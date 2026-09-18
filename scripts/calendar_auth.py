import base64
import hashlib
import json
import os
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

import _bootstrap
from project_paths import CONFIG_DIR


CLIENT_SECRET_FILE = os.environ.get(
    "GOOGLE_CALENDAR_CLIENT_SECRET_FILE",
    str(CONFIG_DIR / "google_calendar_client_secret.json"),
)
TOKEN_FILE = os.environ.get(
    "GOOGLE_CALENDAR_TOKEN_FILE",
    str(CONFIG_DIR / "google_calendar_token.json"),
)
SCOPE = "https://www.googleapis.com/auth/calendar.events"


def load_client():
    if not os.path.exists(CLIENT_SECRET_FILE):
        raise FileNotFoundError(
            "google_calendar_client_secret.jsonがありません"
        )
    with open(CLIENT_SECRET_FILE, encoding="utf-8") as file:
        raw = json.load(file)
    client = raw.get("installed") or raw.get("web")
    if not client:
        raise ValueError("OAuthクライアント設定を読み込めません")
    return client


def main():
    client = load_client()
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    result = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            if query.get("state", [None])[0] != state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write("認証状態が一致しません".encode("utf-8"))
                return
            result["code"] = query.get("code", [None])[0]
            result["error"] = query.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "Google Calendarの認証が完了しました。この画面は閉じてかまいません。".encode("utf-8")
            )

        def log_message(self, _format, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    authorization_uri = client.get(
        "auth_uri", "https://accounts.google.com/o/oauth2/auth"
    )
    url = authorization_uri + "?" + urlencode({
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print("Google Calendarの認証画面を開きます。")
    webbrowser.open(url)
    server.handle_request()
    server.server_close()
    if result.get("error"):
        raise RuntimeError(f"Google認証が失敗しました: {result['error']}")
    if not result.get("code"):
        raise RuntimeError("認証コードを取得できません")

    token_uri = client.get("token_uri", "https://oauth2.googleapis.com/token")
    response = requests.post(
        token_uri,
        data={
            "client_id": client["client_id"],
            "client_secret": client.get("client_secret", ""),
            "code": result["code"],
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    response.raise_for_status()
    token = response.json()
    if not token.get("refresh_token"):
        raise RuntimeError("更新トークンを取得できませんでした")
    stored = {
        "client_id": client["client_id"],
        "client_secret": client.get("client_secret", ""),
        "refresh_token": token["refresh_token"],
        "token_uri": token_uri,
    }
    with open(TOKEN_FILE, "w", encoding="utf-8") as file:
        json.dump(stored, file, ensure_ascii=False, indent=2)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    print("Google Calendarの認証情報を保存しました。")


if __name__ == "__main__":
    main()
