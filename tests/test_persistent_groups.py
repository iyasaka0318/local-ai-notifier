import sqlite3
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from persistent_reminders import (
    add_task,
    complete_group,
    list_active_groups,
)
from reminder_worker import (
    dispatch_persistent_reminders,
    format_group_message,
    group_persistent_tasks,
)
from state_store import ensure_schema


JST = ZoneInfo("Asia/Tokyo")


class GroupBundlingTests(unittest.TestCase):
    def test_grouped_tasks_share_one_bundle(self):
        rows = [
            (1, "牛乳", "t", None, "買い物"),
            (2, "パン", "t", None, "買い物"),
            (3, "郵便を出す", "t", None, None),
        ]
        bundles = group_persistent_tasks(rows)
        self.assertEqual(len(bundles), 2)
        self.assertEqual(bundles[0][0], "買い物")
        self.assertEqual(len(bundles[0][1]), 2)
        self.assertIsNone(bundles[1][0])

    def test_group_message_lists_every_task(self):
        message = format_group_message("買い物", ["牛乳", "パン", "卵"])
        self.assertIn("買い物", message)
        self.assertIn("・牛乳", message)
        self.assertIn("・卵", message)

    def test_single_ungrouped_task_stays_plain(self):
        self.assertEqual(format_group_message(None, ["郵便を出す"]), "郵便を出す")


class PersistentGroupTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ensure_schema(self.conn)
        self.sent = []

    def tearDown(self):
        self.conn.close()

    def sender(self, topic, message, title="リマインダー"):
        self.sent.append((message, title))

    def add(self, note_id, text, group=None):
        task_id = add_task(self.conn, note_id, text, group_name=group)
        self.conn.commit()
        return task_id

    def test_a_shopping_list_arrives_as_one_notification(self):
        """案7: three items must not become three separate alerts."""
        self.add("n1", "牛乳を買う", "買い物")
        self.add("n2", "パンを買う", "買い物")
        self.add("n3", "卵を買う", "買い物")

        now = datetime(2026, 10, 3, 11, 30, tzinfo=JST)
        sent = dispatch_persistent_reminders(
            self.conn, "topic", now=now, sender=self.sender
        )
        self.assertEqual(sent, 1)
        self.assertEqual(len(self.sent), 1)
        message, title = self.sent[0]
        self.assertEqual(title, "買い物")
        for text in ("牛乳を買う", "パンを買う", "卵を買う"):
            self.assertIn(text, message)

    def test_ungrouped_tasks_are_still_sent_separately(self):
        self.add("n1", "牛乳を買う", "買い物")
        self.add("n2", "郵便を出す")
        now = datetime(2026, 10, 3, 11, 30, tzinfo=JST)
        dispatch_persistent_reminders(self.conn, "topic", now=now, sender=self.sender)
        self.assertEqual(len(self.sent), 2)

    def test_a_failed_group_send_is_retried_at_the_next_slot(self):
        self.add("n1", "牛乳を買う", "買い物")
        self.add("n2", "パンを買う", "買い物")

        def failing(topic, message, title="リマインダー"):
            raise RuntimeError("offline")

        now = datetime(2026, 10, 3, 11, 30, tzinfo=JST)
        dispatch_persistent_reminders(self.conn, "topic", now=now, sender=failing)
        slots = [
            row[0] for row in self.conn.execute(
                "SELECT last_notified_slot FROM persistent_reminders ORDER BY id"
            )
        ]
        self.assertEqual(slots, [None, None])

        sent = dispatch_persistent_reminders(
            self.conn, "topic", now=now, sender=self.sender
        )
        self.assertEqual(sent, 1)

    def test_completing_a_group_clears_every_member(self):
        self.add("n1", "牛乳を買う", "買い物")
        self.add("n2", "パンを買う", "買い物")
        self.add("n3", "郵便を出す")

        cleared = complete_group(self.conn, "買い物", command_note_id="n9")
        self.conn.commit()
        self.assertEqual(len(cleared), 2)

        remaining = self.conn.execute(
            "SELECT task_text FROM persistent_reminders WHERE status = 'active'"
        ).fetchall()
        self.assertEqual(remaining, [("郵便を出す",)])

    def test_completing_an_unknown_group_changes_nothing(self):
        self.add("n1", "牛乳を買う", "買い物")
        self.assertEqual(complete_group(self.conn, "掃除"), [])
        self.assertEqual(len(list_active_groups(self.conn)), 1)

    def test_active_groups_are_offered_to_the_classifier(self):
        self.add("n1", "牛乳を買う", "買い物")
        self.add("n2", "傘を持つ", "持ち物")
        self.add("n3", "郵便を出す")
        self.assertEqual(list_active_groups(self.conn), ["持ち物", "買い物"])

    def test_a_duplicate_task_adopts_the_group(self):
        self.add("n1", "牛乳を買う")
        self.add("n2", "牛乳を買う", "買い物")
        rows = self.conn.execute(
            "SELECT group_name FROM persistent_reminders WHERE status = 'active'"
        ).fetchall()
        self.assertEqual(rows, [("買い物",)])


if __name__ == "__main__":
    unittest.main()
