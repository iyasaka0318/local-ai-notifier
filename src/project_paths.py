"""Canonical filesystem locations for the local automation project."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
RUNTIME_DIR = PROJECT_ROOT / "runtime"
BACKUP_DIR = PROJECT_ROOT / "backups"

DB_PATH = str(DATA_DIR / "keep_state.db")


def ensure_runtime_directories():
    for directory in (CONFIG_DIR, DATA_DIR, LOG_DIR, RUNTIME_DIR, BACKUP_DIR):
        directory.mkdir(parents=True, exist_ok=True)
