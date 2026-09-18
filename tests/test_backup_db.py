import sqlite3
import tempfile
import unittest
from pathlib import Path

import backup_database


class BackupDatabaseTests(unittest.TestCase):
    def test_snapshot_is_consistent_and_pruning_keeps_newest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source.db"
            destination = root / "backups"
            conn = sqlite3.connect(source_path)
            conn.execute("CREATE TABLE sample (value TEXT)")
            conn.execute("INSERT INTO sample VALUES ('saved')")
            conn.commit()
            conn.close()

            older = backup_database.create_snapshot(
                source_path, destination, "20260918-010000"
            )
            newer = backup_database.create_snapshot(
                source_path, destination, "20260918-020000"
            )
            backup_database.prune_snapshots(destination, retention=1)

            self.assertFalse(older.exists())
            self.assertTrue(newer.exists())
            restored = sqlite3.connect(newer)
            try:
                self.assertEqual(
                    restored.execute("SELECT value FROM sample").fetchone()[0],
                    "saved",
                )
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
