"""One place that asks the local model for a JSON object.

The model server (Strata) speaks the OpenAI chat API. Its structured output is
schema prompting followed by validation, not constrained decoding, so a reply
can violate the schema. The retry here is what makes that safe for callers that
index straight into the result.
"""

import json
import os
import time

import requests


LLM_URL = os.environ.get(
    "AI_LLM_URL", "http://127.0.0.1:8080/v1/chat/completions"
).strip()
LLM_MODEL = os.environ.get("AI_LLM_MODEL", "strata").strip()
# AI_OLLAMA_THINK is the name this setting had before the server changed.
LLM_THINK = (
    os.environ.get("AI_LLM_THINK") or os.environ.get("AI_OLLAMA_THINK") or "false"
).strip().lower() in {"1", "true", "yes", "on"}
LLM_ATTEMPTS = 3
# The server takes one to three minutes to load the model after a boot, and a
# memo that arrives in that window should wait rather than fail.
LLM_STARTUP_WAIT_SECONDS = int(os.environ.get("AI_LLM_STARTUP_WAIT_SECONDS", "240"))
LLM_STARTUP_POLL_SECONDS = 10


class StructuredOutputError(RuntimeError):
    """The model answered, but never with JSON matching the schema."""


def schema_violation(value, schema, path="$"):
    """Return why ``value`` does not fit ``schema``, or None when it does.

    Covers the subset the workers' schemas use (type, required, properties,
    items, enum); the server only checks that the reply is a JSON object.
    """
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected]
    if expected and not any(_is_type(value, name) for name in types):
        return f"{path}: {expected} ではありません"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path}: 許可されていない値です ({value!r})"
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                return f"{path}.{key}: 必須項目がありません"
        for key, child in schema.get("properties", {}).items():
            if key in value:
                problem = schema_violation(value[key], child, f"{path}.{key}")
                if problem:
                    return problem
    if isinstance(value, list) and len(value) < schema.get("minItems", 0):
        return f"{path}: 要素が足りません"
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, element in enumerate(value):
            problem = schema_violation(element, schema["items"], f"{path}[{index}]")
            if problem:
                return problem
    return None


def _is_type(value, name):
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "null":
        return value is None
    return True


def _post_when_ready(session, payload, timeout, sleep):
    deadline = time.monotonic() + LLM_STARTUP_WAIT_SECONDS
    while True:
        try:
            return session.post(LLM_URL, json=payload, timeout=timeout)
        except requests.ConnectionError:
            if time.monotonic() >= deadline:
                raise
            sleep(LLM_STARTUP_POLL_SECONDS)


def ask_llm(system_prompt, user_data, schema, timeout=300, session=requests,
            sleep=time.sleep):
    user_content = (
        user_data if isinstance(user_data, str)
        else json.dumps(user_data, ensure_ascii=False)
    )
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "answer", "strict": True, "schema": schema},
        },
        "temperature": 0.1,
        "reasoning_effort": "medium" if LLM_THINK else "none",
        "stream": False,
    }

    problem = None
    for _attempt in range(LLM_ATTEMPTS):
        response = _post_when_ready(session, payload, timeout, sleep)
        if response.status_code == 502:
            # The server's own validation rejected the reply; a new generation
            # can succeed. Any other error status is not about the reply.
            problem = _error_text(response)
            continue
        response.raise_for_status()
        try:
            result = json.loads(response.json()["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError) as error:
            problem = f"JSONとして読めません: {error}"
            continue
        problem = schema_violation(result, schema)
        if problem is None:
            return result
    raise StructuredOutputError(
        f"モデルの応答が{LLM_ATTEMPTS}回とも形式に合いませんでした: {problem}"
    )


def _error_text(response):
    try:
        error = response.json().get("error") or {}
        return str(error.get("message") or error.get("code") or error)[:300]
    except ValueError:
        return response.text[:300]
