import argparse
import sqlite3

import _bootstrap
from project_paths import DB_PATH


def main():
    parser = argparse.ArgumentParser(
        description="Show local inbox counts without exposing note contents by default."
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="also print summaries and note IDs",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=5)
    try:
        print("processing states:")
        for status, count in conn.execute("""
            SELECT status, COUNT(*)
            FROM processed_notes
            GROUP BY status
            ORDER BY status
        """):
            print(f"  {status}: {count}")

        for table in ("todos", "calendar_jobs", "reminders", "memos"):
            print(f"\n{table}:")
            if not args.details:
                for status, count in conn.execute(f"""
                    SELECT status, COUNT(*)
                    FROM {table}
                    GROUP BY status
                    ORDER BY status
                """):
                    print(f"  {status}: {count}")
                continue
            rows = conn.execute(f"""
                SELECT note_id, summary, status, updated_at
                FROM {table}
                ORDER BY updated_at DESC
            """).fetchall()
            if not rows:
                print("  (none)")
                continue
            for note_id, summary, status, updated_at in rows:
                print(f"  [{status}] {summary} ({note_id}, {updated_at})")

        print("\nresearch_jobs:")
        for status, count in conn.execute("""
            SELECT status, COUNT(*)
            FROM research_jobs
            GROUP BY status
            ORDER BY status
        """):
            print(f"  {status}: {count}")
        if args.details:
            for job_id, objective, status, updated_at, last_error in conn.execute("""
                SELECT id, objective, status, updated_at, last_error
                FROM research_jobs
                ORDER BY updated_at DESC
            """):
                suffix = f"; error={last_error}" if last_error else ""
                print(f"  [{status}] {objective} (job {job_id}, {updated_at}{suffix})")

        generated_count = conn.execute(
            "SELECT COUNT(*) FROM generated_notes WHERE status = 'created'"
        ).fetchone()[0]
        print(f"\ngenerated Keep notes: {generated_count}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
