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
