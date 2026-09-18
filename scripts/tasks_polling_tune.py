"""Adjust how often Apps Script polls Google Tasks, without redeploying.

Polling only exists as a safety net now: a phone or this machine sends the
wake-up signal itself, because Google's outbound path to ntfy intermittently
hangs for tens of seconds. Those hung polls pile up and saturate the whole
script, which is why the polling rate is worth turning down once the direct
signal is in use.
"""

import argparse

import requests

import _bootstrap  # noqa: F401  (adds src/ to sys.path)

from tasks_client import load_tasks_webhook_credentials


def call(action, **extra):
    credentials = load_tasks_webhook_credentials()
    if not credentials:
        raise RuntimeError("Apps Scriptの接続設定がありません")
    response = requests.post(
        credentials["endpoint_url"],
        json={"action": action, "secret": credentials["secret"], **extra},
        timeout=90,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or f"{action} に失敗しました")
    return result


def show():
    status = call("tasks_polling_status")
    stats = status.get("stats") or {}
    runtime_ms = stats.get("runtime_ms") or 0
    print(f"  トリガー本数   : {status.get('trigger_count')}")
    print(f"  間隔           : {status.get('interval_minutes')} 分")
    print(f"  起動信号URL    : {'設定済み' if status.get('signal_url_configured') else '未設定'}")
    print(f"  本日の実行回数 : {stats.get('count', 0)}")
    print(f"  本日の合計時間 : {runtime_ms / 60000:.1f} 分 / 1日90分の枠")
    print(f"  最長の1回      : {(stats.get('max_runtime_ms') or 0) / 1000:.1f} 秒")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interval", type=int, choices=[1, 5, 10, 15, 30],
        help="ポーリング間隔（分）。Apps Scriptはこの値しか受け付けません",
    )
    parser.add_argument(
        "--triggers", type=int,
        help="トリガー本数（0〜6）。0にするとポーリングを完全に停止します",
    )
    args = parser.parse_args()

    if args.interval is None and args.triggers is None:
        show()
        return

    extra = {}
    if args.interval is not None:
        extra["interval_minutes"] = args.interval
    if args.triggers is not None:
        extra["trigger_count"] = args.triggers
    applied = call("tasks_polling_configure", **extra)
    print(f"  変更しました: {applied['trigger_count']}本 × {applied['interval_minutes']}分間隔")
    if applied["trigger_count"] == 0:
        print("  警告: ポーリングを停止しました。Gemini経由の入力は")
        print("        リスナー起動時と6時間ごとの保険確認でしか拾われません。")


if __name__ == "__main__":
    main()
