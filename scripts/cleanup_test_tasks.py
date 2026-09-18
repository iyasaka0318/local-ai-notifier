"""Find and complete leftover verification tasks in the AI Inbox.

A verification run that cannot read back its own result leaves tasks behind,
and a task left pending in the inbox will be classified and executed on the
next cycle. This lists what is there and completes only what you confirm.
"""

import argparse

import _bootstrap  # noqa: F401  (adds src/ to sys.path)

from tasks_client import get_tasks_inbox_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--match", default="動作確認",
        help="この文字列を含むタスクを対象にします（既定: 動作確認）",
    )
    parser.add_argument(
        "--complete", action="store_true",
        help="実際に完了にします。省略すると一覧表示のみです",
    )
    args = parser.parse_args()

    client = get_tasks_inbox_client()
    items = client.all()
    print(f"AI Inbox の未処理タスク: {len(items)} 件")

    targets = [
        entry for entry in items
        if args.match in (entry.title or "") or args.match in (entry.text or "")
    ]
    if not targets:
        print(f"「{args.match}」を含む未処理タスクはありません。")
        return

    for entry in targets:
        print(f"  - {entry.title!r}  (list: {entry.task_list_title})")

    if not args.complete:
        print("\n完了にするには --complete を付けて再実行してください。")
        return

    for entry in targets:
        client.complete_task(entry.id)
        print(f"  完了にしました: {entry.title!r}")


if __name__ == "__main__":
    main()
