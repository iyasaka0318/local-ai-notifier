"""Send one manual ntfy test notification."""

import os

import requests


def main():
    topic = os.environ["NTFY_TOPIC"]
    response = requests.post(
        "https://ntfy.sh",
        json={
            "topic": topic,
            "title": "ローカルAIテスト",
            "message": "研究室サーバーから通知できています。",
            "priority": 4,
        },
        timeout=30,
    )
    response.raise_for_status()
    print("通知送信OK")


if __name__ == "__main__":
    main()
