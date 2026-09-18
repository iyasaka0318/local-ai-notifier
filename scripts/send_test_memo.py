"""Send one memo through the direct ingest path, as a phone automation would.

Use this to verify the Apps Script deployment before wiring anything on the
phone: if this prints a task id and signalled=True, the phone only has to make
the same HTTPS request.
"""

import argparse
import uuid

import _bootstrap  # noqa: F401  (adds src/ to sys.path)

from tasks_client import get_tasks_inbox_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", help="取り込む本文（音声認識の生テキスト相当）")
    parser.add_argument("--title", default=None, help="タスクのタイトル（省略可）")
    parser.add_argument(
        "--request-id",
        default=None,
        help="再送時の重複防止キー。省略すると毎回新規として扱われます",
    )
    parser.add_argument(
        "--twice",
        action="store_true",
        help="同じrequest_idで2回送り、重複排除が効くか確認します",
    )
    args = parser.parse_args()

    request_id = args.request_id or str(uuid.uuid4())
    client = get_tasks_inbox_client()

    result = client.ingest_text(args.text, args.title, request_id)
    print(f"1回目: task_id={result.get('task_id')} "
          f"signalled={result.get('signalled')} duplicate={result.get('duplicate')}")

    if args.twice:
        again = client.ingest_text(args.text, args.title, request_id)
        print(f"2回目: task_id={again.get('task_id')} "
              f"duplicate={again.get('duplicate')}")
        if again.get("duplicate"):
            print("重複排除は正常に動作しています。")
        else:
            print("警告: 同じrequest_idで2件作成されました。")


if __name__ == "__main__":
    main()
