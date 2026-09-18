import sqlite3
import unittest

from action_log import record_action
from control_listener import handle_message, parse_command
from state_store import ensure_schema, utc_now


class ControlListenerTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ensure_schema(self.conn)
        self.sent = []

    def tearDown(self):
        self.conn.close()

    def sender(self, topic, message, title="リマインダー"):
        self.sent.append((topic, message, title))

    def add_todo(self, note_id="n1"):
        now = utc_now()
        self.conn.execute("""
            INSERT INTO todos (note_id, summary, status, created_at, updated_at)
            VALUES (?, '牛乳を買う', 'pending', ?, ?)
        """, (note_id, now, now))
        return record_action(
            self.conn, source_note_id=note_id, item_id=note_id,
            kind="todo", summary="牛乳を買う",
        )

    def test_parses_a_single_token(self):
        self.assertEqual(parse_command("undo:abc"), ("undo", ["abc"]))

    def test_parses_several_tokens(self):
        self.assertEqual(parse_command("undo:a,b,c"), ("undo", ["a", "b", "c"]))

    def test_ignores_unrelated_messages(self):
        self.assertEqual(parse_command("tasks_changed"), (None, None))
        self.assertEqual(parse_command("undo:"), (None, None))
        self.assertEqual(parse_command(None), (None, None))

    def test_undo_command_cancels_the_todo(self):
        token = self.add_todo()
        handled = handle_message(self.conn, "topic", f"undo:{token}", sender=self.sender)
        self.assertTrue(handled)
        status = self.conn.execute(
            "SELECT status FROM todos WHERE note_id = 'n1'"
        ).fetchone()[0]
        self.assertEqual(status, "cancelled")
        self.assertIn("牛乳を買う", self.sent[0][1])

    def test_unrelated_message_is_not_handled(self):
        self.assertFalse(
            handle_message(self.conn, "topic", "hello", sender=self.sender)
        )
        self.assertEqual(self.sent, [])

    def test_partial_failure_still_reports_every_token(self):
        token = self.add_todo()
        handle_message(
            self.conn, "topic", f"undo:{token},missing", sender=self.sender
        )
        message = self.sent[0][1]
        self.assertIn("牛乳を買う", message)
        self.assertIn("見つかりません", message)


if __name__ == "__main__":
    unittest.main()
