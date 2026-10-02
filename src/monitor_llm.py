"""Model calls shared by web-monitor registration and the monitor worker."""

from llm_client import ask_llm
from web_pages import fetch_page_text


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

    result = ask_llm(
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


VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "confirmed": {"type": "boolean"},
        "evidence": {"type": "string"},
    },
    "required": ["confirmed", "evidence"],
}


VERIFY_PROMPT = """
あなたはWeb監視システムの最終確認担当です。
検索結果の見出しから「目的のページが見つかった」と仮判定されたページの
本文を渡します。本文を読み、ユーザーが待っていたものが本当にこのページに
あるかを判定してください。

- confirmed=true は、本文に目的そのものが明記されている場合だけ。
  例: 参加登録を待っているなら、登録の開始・受付期間・登録フォームへの案内が
  本文にある。
- 回数（第73回など）、年度、用途（参加登録と発表申込など）が依頼と違えば false。
- 「近日公開」「準備中」「後日案内」など、まだ始まっていない記述だけなら false。
- 本文から判断できない場合は false。

evidence には、判定の根拠になった本文の内容を日本語1〜2文で書く。
confirmed=true のときは、ユーザーがすぐ動けるよう、日付・期間・締切などの
要点を含める。本文にない情報は書かない。
"""


def verify_found_page(request_text, candidate, fetch=fetch_page_text, ask=ask_llm):
    """Read the page behind a snippet-level match before the user is notified.

    Returns {"confirmed": True/False/None, "evidence": str}. None means the page
    could not be read, so the caller keeps the snippet-level decision.
    """
    try:
        final_url, page_text = fetch(candidate["url"])
    except Exception as error:
        return {"confirmed": None, "evidence": f"ページを取得できませんでした: {error}"}
    if not page_text.strip():
        return {"confirmed": None, "evidence": "ページ本文を読み取れませんでした"}
    verdict = ask(
        VERIFY_PROMPT,
        {
            "request": request_text,
            "page": {
                "title": candidate.get("title", ""),
                "url": final_url,
                "text": page_text,
            },
        },
        VERIFY_SCHEMA,
    )
    return {
        "confirmed": bool(verdict.get("confirmed")),
        "evidence": (verdict.get("evidence") or "").strip(),
    }
