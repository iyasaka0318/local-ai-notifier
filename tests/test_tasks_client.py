import unittest
from unittest import mock

from tasks_client import TasksInboxClient


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Session:
    def __init__(self):
        self.requests = []

    def post(self, url, json, timeout):
        self.requests.append((url, json, timeout))
        if json["action"] == "tasks_list":
            return Response({
                "ok": True,
                "tasks": [{
                    "id": "remote-1",
                    "task_list_id": "list-1",
                    "task_list_title": "My Tasks",
                    "dedicated_inbox": False,
                    "title": "[AI] AIメモ",
                    "notes": "起きた",
                }],
            })
        return Response({"ok": True, "status": "completed"})


class RetrySession(Session):
    def post(self, url, json, timeout):
        self.requests.append((url, json, timeout))
        if len(self.requests) == 1:
            return Response({}, status_code=404)
        return Response({"ok": True, "tasks": []})


class TasksClientTests(unittest.TestCase):
    def test_list_and_complete_use_prefixed_local_id(self):
        session = Session()
        client = TasksInboxClient(
            credentials={"endpoint_url": "https://example.test/exec", "secret": "s"},
            session=session,
        )

        items = client.all()
        self.assertEqual(items[0].id, "tasks:list-1:remote-1")
        self.assertEqual(items[0].text, "起きた")
        items[0].trash()

        self.assertEqual(session.requests[1][1]["action"], "tasks_complete")
        self.assertEqual(session.requests[1][1]["task_id"], "remote-1")
        self.assertEqual(session.requests[1][1]["task_list_id"], "list-1")
        self.assertFalse(items[0].is_dedicated_inbox)
        self.assertTrue(items[0].trashed)

    @mock.patch("tasks_client.time.sleep")
    def test_transient_apps_script_404_is_retried(self, sleep):
        session = RetrySession()
        client = TasksInboxClient(
            credentials={"endpoint_url": "https://example.test/exec", "secret": "s"},
            session=session,
        )

        self.assertEqual(client.all(), [])
        self.assertEqual(len(session.requests), 2)
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()



class IngestSession:
    def __init__(self, payload=None):
        self.requests = []
        self.payload = payload or {"ok": True, "task_id": "t1", "signalled": True}

    def post(self, url, json, timeout):
        self.requests.append(json)
        return Response(self.payload)


class IngestTests(unittest.TestCase):
    """The direct ingest path a phone automation app uses."""

    def client(self, session):
        return TasksInboxClient(
            credentials={"endpoint_url": "https://example.test", "secret": "s"},
            session=session,
        )

    def test_text_is_sent_verbatim(self):
        session = IngestSession()
        self.client(session).ingest_text("明日9時に歯医者って通知して")
        payload = session.requests[0]
        self.assertEqual(payload["action"], "tasks_ingest")
        self.assertEqual(payload["text"], "明日9時に歯医者って通知して")
        self.assertEqual(payload["secret"], "s")

    def test_optional_fields_are_omitted_when_absent(self):
        session = IngestSession()
        self.client(session).ingest_text("メモ")
        self.assertNotIn("title", session.requests[0])
        self.assertNotIn("request_id", session.requests[0])

    def test_request_id_is_forwarded_for_retry_safety(self):
        session = IngestSession()
        self.client(session).ingest_text("メモ", title="短い題", request_id="abc")
        payload = session.requests[0]
        self.assertEqual(payload["title"], "短い題")
        self.assertEqual(payload["request_id"], "abc")

    def test_failure_is_raised(self):
        session = IngestSession({"ok": False, "error": "認証に失敗しました"})
        with self.assertRaises(RuntimeError):
            self.client(session).ingest_text("x")


if __name__ == "__main__":
    unittest.main()
