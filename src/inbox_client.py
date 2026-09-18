import json
import os

from keep_client import get_authenticated_keep
from project_paths import CONFIG_DIR
from tasks_client import get_tasks_inbox_client


CONFIG_FILE = os.environ.get(
    "AI_INBOX_CONFIG_FILE",
    str(CONFIG_DIR / "inbox_config.json"),
)


def load_input_source():
    environment_value = os.environ.get("AI_INBOX_SOURCE")
    if environment_value:
        return environment_value.strip().lower()
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as file:
            return (json.load(file).get("input_source") or "google_keep").lower()
    return "google_keep"


def get_inbox_client():
    source = load_input_source()
    if source == "google_tasks":
        return get_tasks_inbox_client(), source
    if source == "google_keep":
        return get_authenticated_keep(), source
    raise ValueError(f"未対応のAI Inbox入力元です: {source}")
