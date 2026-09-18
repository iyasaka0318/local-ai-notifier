import sqlite3

import _bootstrap
from project_paths import DB_PATH

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

rows = cur.execute("""
SELECT
    id,
    note_id,
    request_text,
    status,
    found_url
FROM web_monitors
ORDER BY id
""").fetchall()

conn.close()

if not rows:
    print("監視ジョブなし")
else:
    for row in rows:
        print("=" * 60)
        print("ID:", row[0])
        print("note_id:", row[1])
        print("依頼:", row[2])
        print("状態:", row[3])
        print("発見URL:", row[4])
