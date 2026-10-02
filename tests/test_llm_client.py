import unittest
from unittest.mock import Mock

import requests

import llm_client
from llm_client import StructuredOutputError, ask_llm, schema_violation


SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok", "ng"]},
        "at": {"type": ["string", "null"]},
        "items": {"type": "array", "minItems": 1, "items": {
            "type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"],
        }},
    },
    "required": ["status"],
}


def reply(content, status=200):
    response = Mock()
    response.status_code = status
    response.json.return_value = {"choices": [{"message": {"content": content}}]}
    return response


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class SchemaViolationTests(unittest.TestCase):
    def test_a_matching_value_passes(self):
        self.assertIsNone(schema_violation(
            {"status": "ok", "at": None, "items": [{"n": 1}]}, SCHEMA
        ))

    def test_a_missing_required_key_is_reported(self):
        self.assertIn("status", schema_violation({"at": None}, SCHEMA))

    def test_a_value_outside_the_enum_is_reported(self):
        self.assertIn("status", schema_violation({"status": "maybe"}, SCHEMA))

    def test_nested_items_are_checked(self):
        self.assertIn("items[0].n", schema_violation(
            {"status": "ok", "items": [{}]}, SCHEMA
        ))

    def test_an_empty_array_below_min_items_is_reported(self):
        self.assertIn("items", schema_violation({"status": "ok", "items": []}, SCHEMA))

    def test_a_boolean_is_not_an_integer(self):
        self.assertIn("n", schema_violation(
            {"status": "ok", "items": [{"n": True}]}, SCHEMA
        ))


class AskLlmTests(unittest.TestCase):
    def test_thinking_is_off_and_the_schema_is_sent(self):
        session = Session([reply('{"status": "ok"}')])
        result = ask_llm("system", {"input": "テスト"}, SCHEMA, session=session)
        self.assertEqual(result, {"status": "ok"})
        payload = session.calls[0]
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], SCHEMA)
        self.assertIn("テスト", payload["messages"][1]["content"])

    def test_a_schema_violation_is_retried(self):
        session = Session([reply('{"at": null}'), reply('{"status": "ok"}')])
        self.assertEqual(ask_llm("s", "u", SCHEMA, session=session), {"status": "ok"})
        self.assertEqual(len(session.calls), 2)

    def test_the_servers_own_rejection_is_retried(self):
        rejected = reply("", status=502)
        rejected.json.return_value = {"error": {"code": "structured_output_failed"}}
        session = Session([rejected, reply('{"status": "ok"}')])
        self.assertEqual(ask_llm("s", "u", SCHEMA, session=session), {"status": "ok"})

    def test_unreadable_json_is_retried_then_raised(self):
        session = Session([reply("not json")] * llm_client.LLM_ATTEMPTS)
        with self.assertRaises(StructuredOutputError):
            ask_llm("s", "u", SCHEMA, session=session)
        self.assertEqual(len(session.calls), llm_client.LLM_ATTEMPTS)

    def test_a_server_that_is_still_loading_is_waited_for(self):
        session = Session([requests.ConnectionError("refused"), reply('{"status": "ok"}')])
        waits = []
        result = ask_llm("s", "u", SCHEMA, session=session, sleep=waits.append)
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(waits, [llm_client.LLM_STARTUP_POLL_SECONDS])

    def test_a_server_that_never_comes_up_raises(self):
        session = Session([requests.ConnectionError("refused")] * 3)
        original = llm_client.LLM_STARTUP_WAIT_SECONDS
        llm_client.LLM_STARTUP_WAIT_SECONDS = 0
        try:
            with self.assertRaises(requests.ConnectionError):
                ask_llm("s", "u", SCHEMA, session=session, sleep=lambda _s: None)
        finally:
            llm_client.LLM_STARTUP_WAIT_SECONDS = original

    def test_a_server_error_is_not_retried(self):
        failed = reply("", status=500)
        failed.raise_for_status.side_effect = requests.HTTPError("500")
        session = Session([failed, reply('{"status": "ok"}')])
        with self.assertRaises(requests.HTTPError):
            ask_llm("s", "u", SCHEMA, session=session)
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
