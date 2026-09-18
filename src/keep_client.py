import json
import os

import gkeepapi
from project_paths import CONFIG_DIR


CREDENTIAL_FILE = os.environ.get(
    "KEEP_CREDENTIAL_FILE",
    str(CONFIG_DIR / "keep_credentials.json"),
)


def load_keep_credentials():
    if os.path.exists(CREDENTIAL_FILE):
        with open(CREDENTIAL_FILE, encoding="utf-8") as file:
            stored = json.load(file)
        email = stored.get("email")
        master_token = stored.get("master_token")
    else:
        email = os.environ.get("KEEP_EMAIL")
        master_token = os.environ.get("KEEP_MASTER_TOKEN")
    if not email or not master_token:
        raise RuntimeError("Google Keepの認証情報がありません")
    return email, master_token


def get_authenticated_keep():
    email, master_token = load_keep_credentials()
    keep = gkeepapi.Keep()
    keep.authenticate(email, master_token)
    return keep
