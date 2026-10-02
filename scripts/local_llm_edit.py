"""Hand a mechanical edit to the local model and get a diff back.

Meant for an orchestrating agent that wants to spend its own output on the
instruction and the review, not on typing the code:

    scripts/local_llm_edit.py src/a.py -i "各関数にdocstringを付けて"
    scripts/local_llm_edit.py tests/test_new.py --new -c src/a.py -i "a.py のテストを書いて"
    scripts/local_llm_edit.py src/a.py -i "..." --apply

Without --apply nothing is written: the unified diff goes to stdout for review.
The model returns each file whole, so keep targets small enough to rewrite
(the server's context is shared by the files, the context files and the reply).
"""

import argparse
import difflib
import os
import py_compile
import re
import sys
import tempfile
import time
from pathlib import Path

import requests


LLM_URL = os.environ.get(
    "AI_LLM_URL", "http://127.0.0.1:8080/v1/chat/completions"
).strip()
# Rough upper bound for prompt plus reply, in characters, under a 32K context.
MAX_TOTAL_CHARS = int(os.environ.get("AI_LOCAL_EDIT_MAX_CHARS", "48000"))

SYSTEM_PROMPT = """You are a precise code editing tool. You receive an instruction,
the files to edit and optional read-only context files.

Reply with the complete new content of every target file, each in this exact form:

<<<FILE path/as/given>>>
...entire file content...
<<<END>>>

Rules:
- Output every target file in full, from first line to last. Never abbreviate
  with comments such as "rest unchanged".
- Change only what the instruction requires. Keep all other lines byte-identical,
  including comments, blank lines and formatting.
- Do not edit context files and do not output them.
- No explanations, no markdown code fences, nothing outside the FILE blocks.
"""

# Tolerant on purpose: the model sometimes drops the closing >>> of the header
# or the final END marker, and the content is still unambiguous.
BLOCK = re.compile(
    r"<<<FILE ([^\n>]+?)(?:>>>)?[ \t]*\n(.*?)(?:\n?<<<END(?:>>>)?|(?=\n<<<FILE )|\Z)",
    re.S,
)


def build_user_message(instruction, targets, contexts):
    parts = [f"INSTRUCTION:\n{instruction}\n"]
    for path, text in contexts:
        parts.append(f"CONTEXT FILE (read-only) {path}:\n{text}\n")
    for path, text in targets:
        body = text if text is not None else "(this file does not exist yet; create it)"
        parts.append(f"TARGET FILE {path}:\n{body}\n")
    return "\n".join(parts)


def parse_reply(reply, expected_paths):
    """Return {path: content}. Raises ValueError when a target is missing."""
    found = {path.strip(): content for path, content in BLOCK.findall(reply)}
    missing = [path for path in expected_paths if path not in found]
    if missing:
        raise ValueError("モデルの応答にファイルがありません: " + ", ".join(missing))
    return {path: found[path] for path in expected_paths}


def unified_diff(path, before, after):
    return "".join(difflib.unified_diff(
        (before or "").splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    ))


def python_syntax_error(path, content):
    if not path.endswith(".py"):
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
        handle.write(content)
    try:
        py_compile.compile(handle.name, doraise=True)
    except py_compile.PyCompileError as error:
        return str(error.exc_value)
    finally:
        os.unlink(handle.name)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("targets", nargs="+", help="書き換える（または新規作成する）ファイル")
    parser.add_argument("-i", "--instruction")
    parser.add_argument("-f", "--instruction-file", help="指示をファイルから読む（長い指示向け）")
    parser.add_argument("-c", "--context", action="append", default=[],
                        help="読み取り専用で渡すファイル（複数可）")
    parser.add_argument("--new", action="store_true", help="存在しないターゲットを新規作成として許可")
    parser.add_argument("--apply", action="store_true", help="結果をファイルに書き込む")
    parser.add_argument("--think", choices=["none", "low", "medium", "high"], default="none")
    parser.add_argument("--max-tokens", type=int, default=8000,
                        help="応答の上限トークン数（途中で切れたら増やす）")
    args = parser.parse_args()

    if args.instruction_file:
        args.instruction = Path(args.instruction_file).read_text(encoding="utf-8")
    if not args.instruction:
        sys.exit("-i か -f で指示を渡してください")

    targets = []
    for path in args.targets:
        if Path(path).exists():
            targets.append((path, Path(path).read_text(encoding="utf-8")))
        elif args.new:
            targets.append((path, None))
        else:
            sys.exit(f"ファイルがありません（新規作成なら --new）: {path}")
    contexts = [(path, Path(path).read_text(encoding="utf-8")) for path in args.context]

    user_message = build_user_message(args.instruction, targets, contexts)
    reply_room = sum(len(text or "") for _path, text in targets) + 4000
    if len(SYSTEM_PROMPT) + len(user_message) + reply_room > MAX_TOTAL_CHARS:
        sys.exit(
            f"大きすぎます（入力 {len(user_message)} 文字 + 出力見込み {reply_room} 文字）。"
            "ターゲットかコンテキストを減らしてください。"
        )

    started = time.time()
    response = requests.post(LLM_URL, timeout=900, json={
        "model": "strata",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.0,
        "max_tokens": args.max_tokens,
        "reasoning_effort": args.think,
        "stream": False,
    })
    if response.status_code != 200:
        sys.exit(f"HTTP {response.status_code}: {response.text[:400]}")
    body = response.json()
    reply = body["choices"][0]["message"]["content"]
    usage = body.get("usage", {})
    if body["choices"][0].get("finish_reason") == "length":
        sys.exit(
            f"応答が {usage.get('completion_tokens')} トークンで打ち切られました。"
            "--max-tokens を増やすか、作業を分けてください。"
        )

    try:
        results = parse_reply(reply, [path for path, _text in targets])
    except ValueError as error:
        sys.exit(f"{error}\n--- 応答の先頭 ---\n{reply[:600]}")

    problems = []
    for (path, before), content in zip(targets, results.values()):
        content = content if content.endswith("\n") else content + "\n"
        results[path] = content
        diff = unified_diff(path, before, content)
        print(diff if diff else f"(変更なし: {path})")
        error = python_syntax_error(path, content)
        if error:
            problems.append(f"{path}: 構文エラー {error}")

    print(
        f"\n[local-llm] {time.time() - started:.1f}s "
        f"in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')}",
        file=sys.stderr,
    )
    if problems:
        sys.exit("適用しませんでした:\n" + "\n".join(problems))
    if args.apply:
        for path, content in results.items():
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(content, encoding="utf-8")
        print("[local-llm] 適用しました: " + ", ".join(results), file=sys.stderr)


if __name__ == "__main__":
    main()
