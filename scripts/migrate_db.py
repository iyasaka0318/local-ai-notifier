import sqlite3

import _bootstrap

from state_store import ensure_schema
from project_paths import DB_PATH


def main():
    conn = sqlite3.connect(DB_PATH)
    try:
        ensure_schema(conn)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [
            row[0]
            for row in conn.execute("""
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
            """)
        ]
    finally:
        conn.close()

    print("integrity:", integrity)
    print("tables:", ", ".join(tables))


if __name__ == "__main__":
    main()
