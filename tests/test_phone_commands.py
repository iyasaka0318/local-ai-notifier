import json
import sqlite3
import unittest
from datetime import datetime, timedelta

import phone_commands
from state_store import ensure_schema


LOCAL_TZ = phone_commands.LOCAL_TIMEZONE
NOW = datetime(2026, 10, 2, 21, 0, 0, tzinfo=LOCAL_TZ)


def make_conn():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    return conn


class FakeSender:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, topic, action, **fields):
        self.calls.append((topic, action, fields))
        if self.error:
            raise self.error


class FakeResponse:
    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json, timeout))
        return FakeResponse()


class TestAlarmCommand(unittest.TestCase):
    def test_alarm_command_within_direct_window(self):
        payload, send_after, alarm_time = phone_commands.alarm_command(
            "2026-10-03T07:00:00+09:00", "ゴミ出し", now=NOW
        )
        self.assertEqual(payload["seconds"], 25200)
        self.assertEqual(payload["label"], "ゴミ出し")
        self.assertEqual(send_after, NOW)
        self.assertEqual(alarm_time, datetime(2026, 10, 3, 7, 0, 0, tzinfo=LOCAL_TZ))

    def test_alarm_command_far_future_uses_lead(self):
        payload, send_after, alarm_time = phone_commands.alarm_command(
            "2026-10-06T07:30:00+09:00", None, now=NOW
        )
        self.assertEqual(payload["label"], "アラーム")
        self.assertEqual(payload["seconds"], 27000)
        self.assertEqual(send_after, datetime(2026, 10, 5, 19, 30, 0, tzinfo=LOCAL_TZ))

    def test_alarm_command_past_time_rolls_to_next_day(self):
        payload, send_after, alarm_time = phone_commands.alarm_command(
            "2026-10-02T07:00:00+09:00", "x", now=NOW
        )
        self.assertEqual(alarm_time, datetime(2026, 10, 3, 7, 0, 0, tzinfo=LOCAL_TZ))


class TestBuildCommand(unittest.TestCase):
    def test_build_command(self):
        self.assertEqual(
            phone_commands.build_command("ping", "k", a=1),
            {"v": 1, "key": "k", "action": "ping", "a": 1},
        )


class TestPhoneTopic(unittest.TestCase):
    def test_phone_topic(self):
        self.assertEqual(phone_commands.phone_topic("base"), "base-phone")


class TestSendPhoneCommand(unittest.TestCase):
    def test_send_phone_command(self):
        session = FakeSession()
        phone_commands.send_phone_command(
            "base", "alarm_add", session=session, key="k", seconds=60, label="x"
        )
        url, json_arg, timeout = session.calls[0]
        self.assertEqual(json_arg["topic"], "base-phone")
        self.assertEqual(
            json.loads(json_arg["message"]),
            {"v": 1, "key": "k", "action": "alarm_add", "seconds": 60, "label": "x"},
        )


class TestDispatch(unittest.TestCase):
    def test_dispatch_sends_due_command(self):
        conn = make_conn()
        phone_commands.queue_phone_command(
            conn.cursor(), "n1", "n1", "alarm_add", {"seconds": 60, "label": "x", "at": "2026-10-03T00:01:00+09:00"}, NOW, now=NOW
        )
        conn.commit()
        sender = FakeSender()
        result = phone_commands.dispatch_phone_commands(conn, "base", now=NOW, sender=sender)
        self.assertEqual(result, 1)
        self.assertEqual(len(sender.calls), 1)
        topic, action, fields = sender.calls[0]
        self.assertEqual(topic, "base")
        self.assertEqual(action, "alarm_add")
        self.assertEqual(fields["alarms"], [{"seconds": 60, "label": "x"}])
        self.assertEqual(fields["seconds"], 60)
        row = conn.execute("SELECT status FROM phone_commands WHERE note_id = 'n1'").fetchone()
        self.assertEqual(row[0], "sent")

    def test_dispatch_skips_future_command(self):
        conn = make_conn()
        send_after = NOW + timedelta(hours=1)
        phone_commands.queue_phone_command(
            conn.cursor(), "n1", "n1", "alarm_add", {"seconds": 60, "label": "x", "at": "2026-10-03T00:01:00+09:00"}, send_after, now=NOW
        )
        conn.commit()
        sender = FakeSender()
        result = phone_commands.dispatch_phone_commands(conn, "base", now=NOW, sender=sender)
        self.assertEqual(result, 0)
        self.assertEqual(sender.calls, [])
        row = conn.execute("SELECT status FROM phone_commands WHERE note_id = 'n1'").fetchone()
        self.assertEqual(row[0], "pending")

        later = NOW + timedelta(hours=2)
        result = phone_commands.dispatch_phone_commands(conn, "base", now=later, sender=sender)
        self.assertEqual(result, 1)
        self.assertEqual(len(sender.calls), 1)

    def test_dispatch_sender_error(self):
        conn = make_conn()
        phone_commands.queue_phone_command(
            conn.cursor(), "n1", "n1", "alarm_add", {"seconds": 60, "label": "x", "at": "2026-10-03T00:01:00+09:00"}, NOW, now=NOW
        )
        conn.commit()
        sender = FakeSender(error=RuntimeError("boom"))
        result = phone_commands.dispatch_phone_commands(conn, "base", now=NOW, sender=sender)
        self.assertEqual(result, 0)
        row = conn.execute(
            "SELECT status, last_error, attempts FROM phone_commands WHERE note_id = 'n1'"
        ).fetchone()
        self.assertEqual(row[0], "pending")
        self.assertIn("boom", row[1])
        self.assertEqual(row[2], 1)

    def test_dispatch_retries_stale_claim(self):
        conn = make_conn()
        claimed_at = (NOW - timedelta(minutes=10)).isoformat()
        created_at = NOW.isoformat()
        conn.execute(
            "INSERT INTO phone_commands (note_id, source_note_id, action, payload, send_after, status, created_at, claimed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "n1",
                "n1",
                "alarm_add",
                json.dumps({"seconds": 60, "label": "x", "at": "2026-10-03T00:01:00+09:00"}, ensure_ascii=False),
                NOW.isoformat(),
                "sending",
                created_at,
                claimed_at,
            ),
        )
        conn.commit()
        sender = FakeSender()
        result = phone_commands.dispatch_phone_commands(conn, "base", now=NOW, sender=sender)
        self.assertEqual(result, 1)
        self.assertEqual(len(sender.calls), 1)

    def test_due_alarms_go_out_as_one_command(self):
        conn = make_conn()
        phone_commands.queue_phone_command(
            conn.cursor(), "n1", "n1", "alarm_add",
            {"seconds": 25200, "label": "朝", "at": "2026-10-03T07:00:00+09:00"}, NOW, now=NOW
        )
        phone_commands.queue_phone_command(
            conn.cursor(), "n2", "n2", "alarm_add",
            {"seconds": 25500, "label": "朝", "at": "2026-10-03T07:05:00+09:00"}, NOW, now=NOW
        )
        conn.commit()
        sender = FakeSender()
        result = phone_commands.dispatch_phone_commands(conn, "base", now=NOW, sender=sender)
        self.assertEqual(result, 2)
        self.assertEqual(len(sender.calls), 1)
        topic, action, fields = sender.calls[0]
        self.assertEqual(fields["alarms"], [{"seconds": 25200, "label": "朝"}, {"seconds": 25500, "label": "朝"}])
        self.assertEqual(fields["summary"], "10/3 07:00 朝\n10/3 07:05 朝")
        row1 = conn.execute("SELECT status FROM phone_commands WHERE note_id = 'n1'").fetchone()
        row2 = conn.execute("SELECT status FROM phone_commands WHERE note_id = 'n2'").fetchone()
        self.assertEqual(row1[0], "sent")
        self.assertEqual(row2[0], "sent")

    def test_alarm_summary_omits_the_default_label(self):
        result = phone_commands.alarm_summary([{"at": "2026-10-03T07:00:00+09:00", "label": "アラーム", "seconds": 25200}])
        self.assertEqual(result, "10/3 07:00")


class TestCancel(unittest.TestCase):
    def test_cancel_pending(self):
        conn = make_conn()
        phone_commands.queue_phone_command(
            conn.cursor(), "n1", "n1", "alarm_add", {"seconds": 60, "label": "x", "at": "2026-10-03T00:01:00+09:00"}, NOW, now=NOW
        )
        conn.commit()
        result = phone_commands.cancel_phone_command(conn, "n1")
        self.assertEqual(result, "cancelled")
        sender = FakeSender()
        dispatched = phone_commands.dispatch_phone_commands(conn, "base", now=NOW, sender=sender)
        self.assertEqual(dispatched, 0)
        self.assertEqual(sender.calls, [])

    def test_cancel_sent(self):
        conn = make_conn()
        phone_commands.queue_phone_command(
            conn.cursor(), "n1", "n1", "alarm_add", {"seconds": 60, "label": "x", "at": "2026-10-03T00:01:00+09:00"}, NOW, now=NOW
        )
        conn.commit()
        sender = FakeSender()
        phone_commands.dispatch_phone_commands(conn, "base", now=NOW, sender=sender)
        result = phone_commands.cancel_phone_command(conn, "n1")
        self.assertEqual(result, "sent")

    def test_cancel_missing(self):
        conn = make_conn()
        result = phone_commands.cancel_phone_command(conn, "missing")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
