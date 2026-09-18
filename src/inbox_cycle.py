import subprocess
import sys
from pathlib import Path


SOURCE_DIR = Path(__file__).resolve().parent
WORKERS = (
    "watch_keep.py",
    "research_worker.py",
    "calendar_worker.py",
    "wake_worker.py",
    "reminder_worker.py",
)


def run_inbox_cycle():
    """Run one AI Inbox processing cycle and return its combined exit code."""
    exit_code = 0
    for worker in WORKERS:
        completed = subprocess.run(
            [sys.executable, str(SOURCE_DIR / worker)],
            cwd=str(SOURCE_DIR.parent),
            check=False,
        )
        if completed.returncode != 0:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_inbox_cycle())
