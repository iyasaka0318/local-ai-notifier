import os
import json
import sqlite3
import requests
from concurrent.futures import ThreadPoolExecutor

from urllib.parse import urlparse
from ddgs import DDGS
from datetime import datetime
from failure_notifier import notify_processing_failure
from automation_config import OLLAMA_THINK
from instance_lock import SingleInstanceLock
from state_store import ensure_schema, requeue_stale_found_monitors
from project_paths import DB_PATH, RUNTIME_DIR, ensure_runtime_directories


# =========================================================
# 設定
# =========================================================

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3:14b"

NTFY_TOPIC = os.environ["NTFY_TOPIC"]
NTFY_URL = "https://ntfy.sh"


# =========================================================
# Ollama共通
# =========================================================

def ask_ollama(system_prompt, user_data, schema):

    if isinstance(user_data, str):
        user_content = user_data
    else:
        user_content = json.dumps(
            user_data,
            ensure_ascii=False
        )

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": user_content
            }
        ],
        "format": schema,
        "keep_alive": "30m",
        "think": OLLAMA_THINK,
        "stream": False,
        "options": {
            "temperature": 0.1
        }
    }

    response = requests.post(
        OLLAMA_URL,
        json=payload,
        timeout=120
    )

    response.raise_for_status()

    return json.loads(
        response.json()["message"]["content"]
    )


# =========================================================
# Android通知
# =========================================================

def notify_phone(title, message, url=None):

    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": 4
    }

    if url:
        payload["click"] = url

    response = requests.post(
        NTFY_URL,
        json=payload,
        timeout=30
    )

    response.raise_for_status()


# =========================================================
# AIに複数の検索クエリを作らせる
# =========================================================

QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "search_queries": {
            "type": "array",
            "items": {
                "type": "string"
            },
            "minItems": 2,
            "maxItems": 4
        }
    },
    "required": [
        "search_queries"
    ]
}


QUERY_PROMPT = """
あなたはWeb監視システムの検索クエリ作成担当です。
出力する自然言語は、固有名詞を除き必ず日本語にしてください。

ユーザーが待っているページを見つけるための
検索エンジン向け検索語を2～4個作ってください。

同じ意味でも少し異なる検索方法を用意します。

ルール:

- 「監視する」
- 「通知する」
- 「教えて」
- 「リマインド」
- 「公開されたら」
- 「始まったら」

などの依頼表現は検索語から除外する。

イベント名、回数、固有名詞、目的は残す。

1つ目:
短く一般的な検索語。

2つ目:
イベント名など重要部分を引用符で囲んだ
完全一致寄りの検索語。

3つ目以降:
語順や表現を少し変えた検索語。

ユーザーが言っていない組織名や情報を
勝手に追加しない。

検索演算子 site: はここでは使わない。
後続プログラムが自動で追加する。

例:

入力:
第73回EMB研究発表会の参加登録開始を監視するリマインド

出力例:
[
  "第73回EMB研究発表会 参加登録 公式",
  "\\"第73回EMB研究発表会\\" 参加登録",
  "第73回 EMB 研究発表会 参加登録"
]
"""


def generate_search_queries(request_text):

    result = ask_ollama(
        QUERY_PROMPT,
        request_text,
        QUERY_SCHEMA
    )

    queries = []

    for q in result["search_queries"]:

        q = q.strip()

        if q and q not in queries:
            queries.append(q)

    return queries


# =========================================================
# 既知の監視URLから公式候補ドメインを取得
# =========================================================

def get_known_domains(monitor_urls_json):

    domains = []

    try:
        urls = json.loads(
            monitor_urls_json or "[]"
        )

    except Exception:
        urls = []

    for url in urls:

        try:

            hostname = urlparse(url).hostname

            if not hostname:
                continue

            hostname = hostname.lower()

            if hostname.startswith("www."):
                hostname = hostname[4:]

            if hostname not in domains:
                domains.append(hostname)

        except Exception:
            pass

    return domains


# =========================================================
# 複数検索して重複除去
# =========================================================

def search_web(queries):
    def run_query(query):
        try:
            return query, list(DDGS().text(query, max_results=8))
        except Exception as error:
            print("検索失敗:")
            print(error)
            return query, []

    by_url = {}
    if not queries:
        return []
    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as executor:
        for query, results in executor.map(run_query, queries):
            print()
            print("検索:")
            print(query)
            for result in results:

                url = result.get(
                    "href",
                    ""
                ).strip()

                if not url:
                    continue

                # URL単位で重複除去
                if url in by_url:
                    continue

                by_url[url] = {
                    "title": result.get(
                        "title",
                        ""
                    ),
                    "url": url,
                    "description": result.get(
                        "body",
                        ""
                    ),
                    "found_by_query": query
                }


    candidates = []

    for i, item in enumerate(
        by_url.values()
    ):

        candidates.append({
            "id": i,
            **item
        })

    return candidates


# =========================================================
# 検索結果判定
# =========================================================

TARGET_SCHEMA = {
    "type": "object",
    "properties": {
        "target_found": {
            "type": "boolean"
        },
        "target_result_id": {
            "type": [
                "integer",
                "null"
            ]
        },
        "reason": {
            "type": "string"
        }
    },
    "required": [
        "target_found",
        "target_result_id",
        "reason"
    ]
}


TARGET_PROMPT = """
あなたはWeb監視システムの最終判定担当です。
ユーザー向けの自然言語は、固有名詞を除き必ず日本語にしてください。

ユーザーが待っている目的のページが
検索結果に出現したか判定してください。

非常に慎重に判定してください。

ルール:

- 検索結果に存在しないURLを作らない。
- 検索結果のtitle、url、descriptionだけを根拠にする。
- 目的を明確に満たす場合だけtarget_found=true。
- 少しでも不確実ならfalse。
- 公式サイトを優先する。
- 非公式まとめサイトやSNSだけでは原則trueにしない。

回数を厳密に区別する。

例えばユーザーが
「第73回」を求めている場合、

第72回
第71回
第70回

などは絶対に目的ページではない。

用途も厳密に区別する。

例えば:

参加登録
発表申込
論文投稿
原稿提出
演題登録

は別物。

ユーザーが「参加登録」を待っている場合、
発表者向け申込ページを目的ページとして扱わない。

イベント個別ページそのものに
「参加登録開始」や参加登録リンクが
明確に記載されている場合はtrueとしてよい。

target_result_idには
検索結果idを返す。

見つからない場合:

target_found=false
target_result_id=null

reasonは日本語で簡潔に説明する。
"""


def judge_results(
    request_text,
    candidates
):

    return ask_ollama(
        TARGET_PROMPT,
        {
            "request": request_text,
            "search_results": candidates
        },
        TARGET_SCHEMA
    )


# =========================================================
# DB
# =========================================================

ensure_runtime_directories()
instance_lock = SingleInstanceLock(str(RUNTIME_DIR / "monitor_worker.lock"))
if not instance_lock.acquire():
    print("Web monitor worker is already running; this invocation will exit.")
    raise SystemExit(0)

conn = sqlite3.connect(
    DB_PATH,
    timeout=30,
)
ensure_schema(conn)
# A crash between claiming 'found' and delivering the alert would otherwise
# swallow the discovery, so re-arm anything that never reached the phone.
requeue_stale_found_monitors(conn)

cur = conn.cursor()


cur.execute("""
SELECT
    id,
    request_text,
    search_query,
    monitor_urls
FROM web_monitors
WHERE status = 'active'
""")


jobs = cur.fetchall()


if not jobs:

    print(
        "現在、監視中のジョブはありません"
    )

    conn.close()

    raise SystemExit


print(
    f"{len(jobs)}件の監視ジョブを確認します"
)


# =========================================================
# 各監視ジョブ
# =========================================================

for (
    job_id,
    request_text,
    old_search_query,
    monitor_urls_json
) in jobs:

    print()
    print("=" * 60)

    print(
        f"監視ジョブ #{job_id}"
    )

    print(
        "依頼:",
        request_text
    )


    # -----------------------------------------------------
    # AIに検索クエリを複数生成
    # -----------------------------------------------------

    try:

        search_queries = (
            generate_search_queries(
                request_text
            )
        )

    except Exception as e:

        print(
            "検索クエリ生成失敗:"
        )

        print(e)

        notify_processing_failure(
            conn, NTFY_TOPIC, "Web監視の検索準備", job_id, e, retrying=True
        )

        continue


    # -----------------------------------------------------
    # 既知の公式候補ドメイン
    # -----------------------------------------------------

    known_domains = get_known_domains(
        monitor_urls_json
    )


    # -----------------------------------------------------
    # 公式候補ドメイン限定検索も追加
    # -----------------------------------------------------

    all_queries = list(
        search_queries
    )


    if search_queries:

        base_query = search_queries[0]

        for domain in known_domains:

            domain_query = (
                f"{base_query} "
                f"site:{domain}"
            )

            if (
                domain_query
                not in all_queries
            ):
                all_queries.append(
                    domain_query
                )


    # -----------------------------------------------------
    # 最大6検索まで
    # -----------------------------------------------------

    all_queries = all_queries[:6]


    print()
    print("今回使う検索クエリ:")

    for q in all_queries:
        print(" -", q)


    # メイン検索語をDB保存
    if search_queries:

        cur.execute("""
        UPDATE web_monitors
        SET search_query = ?
        WHERE id = ?
        """, (
            search_queries[0],
            job_id
        ))

        conn.commit()


    # -----------------------------------------------------
    # 検索
    # -----------------------------------------------------

    print()
    print("Web検索開始")

    candidates = search_web(
        all_queries
    )


    if not candidates:

        print()
        print(
            "検索結果がありません"
        )

        cur.execute("""
        UPDATE web_monitors
        SET last_checked_at = ?
        WHERE id = ?
        """, (
            datetime.now().isoformat(),
            job_id
        ))

        conn.commit()

        continue


    # -----------------------------------------------------
    # 結果表示
    # -----------------------------------------------------

    print()
    print(
        f"重複除去後: "
        f"{len(candidates)}件"
    )

    print()

    for candidate in candidates:

        print(
            f"[{candidate['id']}] "
            f"{candidate['title']}"
        )

        print(
            candidate["url"]
        )


    # -----------------------------------------------------
    # AI判定
    # -----------------------------------------------------

    print()
    print("AI判定中...")


    try:

        decision = judge_results(
            request_text,
            candidates
        )

    except Exception as e:

        print(
            "AI判定失敗:"
        )

        print(e)

        notify_processing_failure(
            conn, NTFY_TOPIC, "Web監視の判定", job_id, e, retrying=True
        )

        continue


    print()
    print("判定結果:")

    print(
        json.dumps(
            decision,
            ensure_ascii=False,
            indent=2
        )
    )


    # -----------------------------------------------------
    # 発見
    # -----------------------------------------------------

    if decision["target_found"]:

        idx = decision[
            "target_result_id"
        ]

        if (
            idx is None
            or idx < 0
            or idx >= len(candidates)
        ):

            print()
            print(
                "result_idが不正なので"
                "発見判定を無視します"
            )

            continue


        found = candidates[idx]

        # Claim completion before notification. If the monitor was cancelled
        # after the initial SELECT, this guarded transition fails and no stale
        # notification is sent.
        found_at = datetime.now().isoformat()
        cur.execute("""
        UPDATE web_monitors
        SET status = 'found', found_url = ?, last_checked_at = ?
        WHERE id = ? AND status = 'active'
        """, (found["url"], found_at, job_id))
        conn.commit()
        if cur.rowcount == 0:
            print("監視は既に取り消されているため通知をスキップします")
            continue


        print()
        print("=" * 60)
        print(
            "目的ページを発見しました！"
        )
        print("=" * 60)

        print(
            found["title"]
        )

        print(
            found["url"]
        )


        # -------------------------------------------------
        # Android通知
        # -------------------------------------------------

        try:

            notify_phone(
                "Web監視：目的ページを発見",
                (
                    f"監視していたページを発見しました。\n\n"
                    f"目的: {request_text}\n"
                    f"ページ: {found['title']}"
                ),
                found["url"]
            )

            print()
            print(
                "Androidへ通知しました"
            )

        except Exception as e:

            print()
            print(
                "通知送信失敗:"
            )

            print(e)

            # Delivery did not happen, so make this exact claimed result
            # eligible for a later retry without reviving a cancelled row.
            cur.execute("""
            UPDATE web_monitors
            SET status = 'active', found_url = NULL
            WHERE id = ? AND status = 'found' AND found_url = ?
            """, (job_id, found["url"]))
            conn.commit()

            notify_processing_failure(
                conn, NTFY_TOPIC, "Web監視の完了通知", job_id, e, retrying=True
            )

            # 通知できなかったら
            # jobはactiveのまま
            continue


        cur.execute(
            "UPDATE web_monitors SET notified_at = ? WHERE id = ?",
            (datetime.now().isoformat(), job_id),
        )
        conn.commit()

        print(
            "監視ジョブを完了しました"
        )


    # -----------------------------------------------------
    # まだ見つからない
    # -----------------------------------------------------

    else:

        print()
        print(
            "まだ目的ページはありません"
        )

        cur.execute("""
        UPDATE web_monitors
        SET last_checked_at = ?
        WHERE id = ? AND status = 'active'
        """, (
            datetime.now().isoformat(),
            job_id
        ))

        conn.commit()


# =========================================================
# 終了
# =========================================================

conn.close()

print()
print("=" * 60)
print("監視チェック完了")
print("=" * 60)
