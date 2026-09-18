import json
import os
import tempfile
import unittest
from unittest import mock

import tasks_event_listener


class TasksEventListenerTests(unittest.TestCase):
    def test_load_signal_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "tasks_event.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump({"signal_url": "https://ntfy.sh/private-topic/"}, file)
            with mock.patch.object(tasks_event_listener, "CONFIG_FILE", path):
                self.assertEqual(
                    tasks_event_listener.load_signal_url(),
                    "https://ntfy.sh/private-topic",
                )

    def test_rejects_non_https_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "tasks_event.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump({"signal_url": "http://example.test/topic"}, file)
            with mock.patch.object(tasks_event_listener, "CONFIG_FILE", path):
                with self.assertRaises(RuntimeError):
                    tasks_event_listener.load_signal_url()


if __name__ == "__main__":
    unittest.main()
