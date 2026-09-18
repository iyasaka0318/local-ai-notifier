"""Create consistent SQLite snapshots while workers are running."""

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from project_paths import BACKUP_DIR, DB_PATH


RETENTION = int(os.environ.get("AI_BACKUP_RETENTION", "30"))
EXTERNAL_BACKUP_DIR = os.environ.get("AI_EXTERNAL_BACKUP_DIR")


def create_snapshot(source_path, destination_dir, timestamp=None):
    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=True)
    stamp = timestamp or datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    final_path = destination / f"keep_state.backup-linux-{stamp}.db"
    temporary_path = destination / f".{final_path.name}.tmp"

    source_uri = Path(source_path).resolve().as_uri() + "?mode=ro"
    source = None
    target = None
    failed = False
    try:
        temporary_path.unlink(missing_ok=True)
        source = sqlite3.connect(source_uri, uri=True, timeout=30)
        target = sqlite3.connect(temporary_path, timeout=30)
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"backup integrity check failed: {integrity}")
    except Exception:
        failed = True
        raise
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
        if failed:
            temporary_path.unlink(missing_ok=True)

    temporary_path.chmod(0o600)
    temporary_path.replace(final_path)
    return final_path


def prune_snapshots(destination_dir, retention=RETENTION):
    snapshots = sorted(
        Path(destination_dir).glob("keep_state.backup-linux-*.db"),
        reverse=True,
    )
    for obsolete in snapshots[max(1, retention):]:
        obsolete.unlink()


def main():
    local_path = create_snapshot(DB_PATH, BACKUP_DIR)
    prune_snapshots(BACKUP_DIR)
    print(f"Local backup created: {local_path.name}")

    if EXTERNAL_BACKUP_DIR:
        try:
            external_path = create_snapshot(DB_PATH, EXTERNAL_BACKUP_DIR)
            prune_snapshots(EXTERNAL_BACKUP_DIR)
            print(f"External backup created: {external_path.name}")
        except (OSError, RuntimeError, sqlite3.Error) as error:
            print(f"External backup skipped: {type(error).__name__}")
