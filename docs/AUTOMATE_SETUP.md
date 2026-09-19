# Automate から直接入力する（Gemini を経由しない）

電源ボタン長押しの音声入力はそのままに、入力の届け先を Gemini から Automate に
差し替える手順です。Gemini の言い換えがなくなり、発話から2秒ほどでPCが動き出します。

| | Gemini 経由（現在） | Automate 直送 |
|---|---|---|
| 発話 | 「AIメモ、〜、AIメモ」 | 用件だけ |
| 原文の保持 | 言い換え・日時補完が起きることがある | 音声認識の生テキストがそのまま |
| PCが動き出すまで | PC側の定期確認（既定15分） | 2秒程度（実測・スマホから直接合図） |
| 届いた確認 | ローカルの処理完了まで分からない | 発話直後にバイブ |
| 圏外時 | 失敗しても気づけない | Automate 側で再送できる |

「AIメモ」で囲む必要がなくなるのは、あの合図が Gemini にこのフローを使わせるための
ものだからです。AI Inbox リストに入ったタスクは、マーカーがなくても
`dedicated_inbox` として信頼されます。

## 1. PC から先に動作確認する

スマホを触る前に、PC から同じリクエストを送って確認します。ここが通らないうちは
スマホ側に進まないでください。原因の切り分けが難しくなります。

```bash
./runtime/run-worker.sh scripts/send_test_memo.py "テスト、明日の10時に歯医者って通知して" --twice
```

`signalled=True` が出れば、取り込みと同時にローカルへ合図が飛んでいます。
`--twice` は同じ `request_id` で2回送り、重複排除が効くことを確認します。
2回目が `duplicate=True` になれば正常です。

実測では 2.1 秒 / `duplicate=True` が返ります。

## 2. Automate のフローを作る

ブロック名とフィールド名は[公式ドキュメント](https://llamalab.com/automate/doc/block/index.html)で確認済みのものです。

### 2-1. 先に2つの値を手元に用意する

```bash
cat config/calendar_webhook.json   # endpoint_url と secret
cat config/tasks_event.json        # signal_url
```

この3つをスマホにコピーしておきます。**secret と signal_url は鍵と同等**なので、送信手段には注意してください。

### 2-2. 新しいフローを作る

Automate を開き、右下の **＋** → 空のフローを作成。`Flow beginning` が1個だけ置かれた状態から始めます。

ブロックの追加は、キャンバス長押し → カテゴリ選択 → ブロック選択です。接続は、ブロック下部の丸（出力）をドラッグして次のブロックの上部の丸（入力）へ落とします。

### 2-3. ブロックを並べる

以下の順に12個置きます。`→` は接続先です。

| # | ブロック | 設定 |
|---|---|---|
| 1 | `Flow beginning` | そのまま |
| 2 | `Assist request` | **Title** に「AIメモ」など |
| 3 | `Speech recognition` | `Spoken texts` 欄に `spoken` と入力 |
| 4 | `Expression true` | 発話が取れたか |
| 5 | `Variable set` ×2 | `text` と `requestId` |
| 6 | `File write` | 未送信キューの保存 |
| 7 | `Failure catch` | **Retry limit** = 2 |
| 8 | `HTTP request` ①取り込み | **Timeout 60** |
| 9 | `Variable set` | `res = jsonDecode(body)` |
| 10 | `Expression true` | 保存できたか |
| 11 | `Vibrate` ＋ `File write` | 手応え＋キュー削除 |
| 12 | `HTTP request` ②起動合図 | 失敗しても再送しない |

**配線の要点は3つです。**

- **2番（Assist request）がループの先頭です。** このブロックは「アシスト要求が来るまで一時停止」する仕様なので、処理が終わったら必ず2番へ戻してください。戻し忘れると**1回しか動きません**。
- **7番（Failure catch）は8番より前に置きます。** このブロックが捕まえるのは「自分より後ろのブロック」の失敗だけです。`FAIL` 出口は11番ではなく「通知を出して2番へ戻る」に繋いでください。
- **4番・10番の否定側出口も2番へ戻します。** 行き止まりにすると、失敗のたびにフローが死んでアシスト呼び出しに応答しなくなります。

### 2-4. Automate の式の書き方

**ここを知らずに書くと必ず弾かれます。**

| よくある書き方 | Automate での正解 |
|---|---|
| `a == b` | **`a = b`** |
| `a and b` | **`a && b`** |
| `a or b` | **`a \|\| b`** |

配列と辞書はどちらも `[ ]` で取り出します（`["a","b"][1]` → `"b"`、`{"a":1}["a"]` → `1`）。
`null`・`0`・空文字・空配列・空辞書はすべて偽として扱われるので、`x != null` ではなく
`x` だけで「中身がある」を判定できます。

### 2-5. 各ブロックの中身

**3番 Speech recognition**

`Spoken texts` は変数名ではなく、**結果を入れる変数の名前を自分で書き込む欄**です。
公式の説明は "variable to assign an array of interpretation alternatives"。

| 欄 | 値 |
|---|---|
| Spoken texts | `spoken` と入力（名前は任意） |
| Confidence scores | 空のまま |
| Offline | オフ（オンにすると認識精度が落ちます） |

中身は**認識候補の配列**です。文字列としてそのまま送ると失敗します。

**4番 Expression true**（発話が取れたか）

```
spoken && spoken[0] != ""
```

出口は `YES` / `NO` の2本。`NO` は 2 へ戻します。

**5番 Variable set ×2**

| Variable | Value |
|---|---|
| `text` | `spoken[0]` |
| `requestId` | `uuid4()` |

`requestId` は**発話ごとに1回だけ**作ります。再送のたびに作り直すと二重登録されます。

**6番 File write**（未送信キュー）

| 欄 | 値 |
|---|---|
| File | `/sdcard/Automate/ai-inbox-unsent.json` |
| Content | `jsonEncode({"requestId": requestId, "text": text})` |
| Append | オフ（上書き） |

送信より**前**に保存します。HyperOS に止められても次の起動時に再送できます。

**7番 Failure catch**

`Retry limit` = 2。`FAIL` 出口 → `Notification show`「保存できませんでした」→ 2 へ戻す。

**8番 HTTP request（①取り込み）**

| フィールド | 値 |
|---|---|
| Request URL | `config/calendar_webhook.json` の `endpoint_url` |
| Request method | `POST` |
| Request content type | `application/json` |
| Request content body | 下記 |
| **Timeout** | **60** ← 既定15秒では足りません |
| **Save response** | **`Don't save`（既定のまま）** |
| Response status code | `status` と入力 |
| Response content | `body` と入力 |

`Save to file` にすると `Response content` に入るのは本文ではなく**ファイルのパス**で、
`jsonDecode` が失敗します。既定のままにしてください。

```
jsonEncode({
  "secret":     "<calendar_webhook.json の secret>",
  "action":     "tasks_ingest",
  "text":       text,
  "request_id": requestId
})
```

**9番 Variable set**

| Variable | Value |
|---|---|
| `res` | `jsonDecode(body)` |

一度変数に置いてから取り出します。式の中で関数の結果に直接 `[ ]` を付けるより確実で、
2回パースする無駄もありません。

**10番 Expression true**（保存できたか）

```
(status = 200) && res["ok"] && res["task_id"]
```

`= 200` は `&&` との優先順位を確実にするため括弧で囲みます。

**Apps Script は認証失敗も HTTP 200 の JSON で返します。** ステータスだけ見ると、
保存できていないのにバイブが鳴ります。ここが最も間違えやすい箇所です。

**11番 Vibrate + File write**

バイブで手応えを返し、`File write` で未送信キューを空にします（`Content` を `""`、`Append` オフ）。

**12番 HTTP request（②起動合図）**

| フィールド | 値 |
|---|---|
| Request URL | `config/tasks_event.json` の `signal_url` |
| Request method | `POST` |
| Request content type | `text/plain` |
| Request content body | `"tasks_changed"` |
| Timeout | 15（既定のまま） |

**②は失敗しても再送しません。** ①が通っていれば保存は済んでいて、②の失敗は
「PCが動き出すのが遅れる」だけです。PC側の定期確認（既定15分）が拾います。

### 2-6. 起動時に未送信を再送する（任意だが推奨）

別フローを1つ作り、`Flow beginning` → `File read` → 中身があれば 8番と同じ `HTTP request` を実行、という形にします。**保存済みの `requestId` をそのまま使う**ので、既に登録されていた場合は `duplicate: true` が返るだけで二重登録されません。

Automate の設定で、このフローを端末起動時に自動実行するようにしておきます。

### 2-7. アシスタントとして登録する

```
設定 → アプリ → 標準のアプリ → デジタルアシスタントアプリ → Automate
```

電源ボタン長押しがアシスタント呼び出しに割り当たっていることも確認してください。

```
設定 → 追加設定 → ボタンショートカット → 電源ボタンを長押し
```

## 3. Xiaomi（HyperOS / MIUI）での必須設定

**これを入れないと数時間で無言で動かなくなります。** HyperOS のプロセス管理は
バックグラウンドのアプリを強く制限するため、Automate は明示的に除外が必要です。

```
設定 → アプリ → アプリを管理 → Automate
    自動起動      → オン
    省電力        → 制限なし

設定 → バッテリー → 省電力モード時のアプリ制御
    Automate     → 制限なし

最近のアプリ画面 → Automate のカードを下にスワイプ → 🔒
```

止められたこと自体は Automate では検知できません。だから 2-6 の起動時再送と、
①成功時のバイブ（＝保存できた手応え）の両方が必要になります。

## 4. この内容の根拠

ブロック名・フィールド名・演算子はすべて公式リファレンスで確認しています。

| 確認したこと | 出典 |
|---|---|
| `Spoken texts` は「割り当て先の変数」 | [Speech recognition](https://llamalab.com/automate/doc/block/speech_recognition.html) |
| 等価は `=`、論理積は `&&` | [Expressions & operators](https://llamalab.com/automate/doc/expression.html) |
| 配列・辞書はどちらも `[ ]` | [Expressions & operators](https://llamalab.com/automate/doc/expression.html) |
| `Timeout` 既定15秒、`Save response` 既定 `Don't save` | [HTTP request](https://llamalab.com/automate/doc/block/http_request.html) |
| `Failure catch` は後続ブロックのみ保護 | [Failure catch](https://llamalab.com/automate/doc/block/failure_catch.html) |
| `Assist request` は要求が来るまで一時停止 | [Assist request](https://llamalab.com/automate/doc/block/assist_request.html) |
| `text uuid4()` は引数なし | [uuid4](https://llamalab.com/automate/doc/function/uuid4.html) |
| `File write` の欄は `File` / `Content` / `Append` | [File write](https://llamalab.com/automate/doc/block/file_write.html) |

それでも式エディタが受け付けない場合は、エラーメッセージをそのまま知らせてください。

## 5. Gemini 経由は残しておく

`tasks_ingest` は既存の経路に何も影響しません。Gemini からの投入もそのまま動きます。
Automate が不調なときの逃げ道として、しばらく併用してください。

Gemini 経由の入力は、**PC側の定期確認**（既定15分・`AI_FALLBACK_CHECK_SECONDS`）が
回収します。Apps Script のポーリングは停止済みです。Google の送信元から ntfy への
接続が断続的に数十秒ハングし、その間 Apps Script への他のリクエストまで遅くなって
いたためで、この経路はシステムから外してあります。

どちらの経路で入ったかは次で確認できます。

```bash
./runtime/run-worker.sh scripts/show_inbox.py --details
```
