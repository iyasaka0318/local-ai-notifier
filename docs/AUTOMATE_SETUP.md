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

ブロック名・欄の名前は[公式ドキュメント](https://llamalab.com/automate/doc/block/index.html)で確認済みのものです。
番号は**ブロック1個につき1つ**です。同じ種類のブロックも縦に1個ずつ並べます。

### 2-1. 先に用意するもの

**保存フォルダ** `/sdcard/Download/automate`（他のファイルを置かないこと。中身は全部「未送信メモ」として送られます）。
ファイルは8番が自動で作ります。初回に「すべてのファイルへのアクセス」を求められたら許可します。

**PC の設定ファイルにある3つの値**

```bash
cat config/calendar_webhook.json   # endpoint_url（https://script.google.com/macros/s/…/exec）と secret（64文字）
cat config/tasks_event.json        # signal_url（https://ntfy.sh/…）
```

Gmail の下書きや Google Keep 経由でスマホに渡し、**貼り終わったら消します**。
`secret` と `signal_url` は鍵と同等です。前後の `"` はコピーしません。

### 2-2. fx ボタン（式モードと定数モード）

入力欄の fx アイコン（取り消し線付き）を押すと `=` が出て**式モード**になります。もう一度押すと**定数モード**に戻ります。

| 印 | 意味 |
|---|---|
| `=` | fx を押して式モードにしてから書く（`jsonEncode(…)`、`spoken[0]` など） |
| 定数 | fx は押さず、文字をそのまま書く（URL、`application/json` など） |
| 変数名 | 結果を入れる変数の名前を書く欄。fx はない |

定数モードのまま `jsonEncode({…})` を書くと保存できません。

式の書き方：等価は `=`、論理積は `&&`、文字の連結は `++`。配列も辞書も `[ ]` で取り出します。
null・0・空文字・空配列は偽なので、`x` だけで「中身がある」を判定できます。

### 2-3. ブロックと配線

| # | ブロック | 設定 | 出口 |
|---|---|---|---|
| 1 | `Flow beginning` | そのまま | → 2 |
| 2 | `Failure catch` | Retry limit 定数 `1000` | → 3 ／ FAIL → 19 |
| 3 | `Assist request` | Title 定数 `AIメモ` | → 4 |
| 4 | `Speech recognition` | Spoken texts 変数名 `spoken`、Offline オフ | → 5 |
| 5 | `Expression true` | `=` `spoken && spoken[0] != ""` | YES → 6 ／ NO → 3 |
| 6 | `Variable set` | Variable `text`、Value `=` `spoken[0]` | → 7 |
| 7 | `Variable set` | Variable `requestId`、Value `=` `uuid4()` | → 8 |
| 8 | `File write` | File `=` `"/sdcard/Download/automate/" ++ requestId ++ ".json"`、Content `=` `jsonEncode({"requestId": requestId, "text": text})`、Append オフ | → 9 |
| 9 | `File list` | Path 定数 `/sdcard/Download/automate`、Filenames 変数名 `files` | → 10 |
| 10 | `For each` | Container `=` `files`、Entry value 変数名 `path` | DO → 11 ／ OK → 3 |
| 11 | `File read` | File `=` `path`、Text content 変数名 `saved` | → 12 |
| 12 | `Variable set` | Variable `q`、Value `=` `jsonDecode(saved)` | → 13 |
| 13 | `HTTP request`（取り込み） | 下記 | → 14 |
| 14 | `Variable set` | Variable `res`、Value `=` `jsonDecode(body)` | → 15 |
| 15 | `Expression true` | `=` `(status = 200) && res["ok"] && res["task_id"]` | YES → 16 ／ NO → 19 |
| 16 | `File delete` | Path `=` `path`、Recursive オフ | → 17 |
| 17 | `Vibrate` | 既定のまま | → 18 |
| 18 | `HTTP request`（起動合図） | 下記 | → 10 |
| 19 | `Notification show` | Title 定数 `AIメモ送信失敗`、Message 定数 `次に話したときに再送します` | → 3 |

線は必ず「出口の丸（OK / YES / NO / DO / FAIL）→ 相手の `IN`」の向きに引きます。丸の位置は公式に
記載がないので、丸の横の名前で見分けます。`IN` には複数の線を入れられます。

- **3番の IN に4本**：2番 OK、5番 NO、10番 OK、19番 OK
- **10番の IN に2本**：9番 OK、18番 OK（**18番は3番ではなく10番へ**。3番に繋ぐと2件目以降が送られない）
- **19番の IN に2本**：2番 FAIL、15番 NO
- 2番の FAIL は「3〜18番のどこかが失敗したとき」に通る出口。13番・18番から失敗用の線を引く必要はない

For each（10番）は `files` を1件ずつ `path` に入れて DO から出し、18番から IN に戻ると次の1件を出し、
全部終わると OK から出ます。

**13番 HTTP request（取り込み）**

| 欄 | 値 |
|---|---|
| Request URL | 定数 `endpoint_url` の値 |
| Request method | `POST` |
| Request content type | 定数 `application/json` |
| Request content body | `=` 下記（`ここにsecret` を置き換える） |
| Timeout | 定数 **60**（既定15秒では足りません） |
| Save response | **`Don't save`（既定のまま）** |
| Response status code | 変数名 `status` |
| Response content | 変数名 `body` |

```
jsonEncode({"secret": "ここにsecret", "action": "tasks_ingest", "text": q["text"], "request_id": q["requestId"]})
```

`Save to file` にすると `body` に本文ではなくファイルのパスが入り、14番が失敗します。
**Apps Script は認証失敗も HTTP 200 で返す**ので、15番で `res["ok"]` まで確認します。

**18番 HTTP request（起動合図）**

| 欄 | 値 |
|---|---|
| Request URL | 定数 `signal_url` の値 |
| Request method | `POST` |
| Request content type | 定数 `text/plain` |
| Request content body | 定数 `tasks_changed`（**`"` を付けない**） |

PC側は本文が `tasks_changed` と完全一致するときだけ動きます。定数モードで `"tasks_changed"` と書くと引用符まで送られ、無視されます。

### 2-4. この形にしている理由

- **保存してから送る。** 8番で1件1ファイルに保存し、9〜18番でフォルダに残る全件を送って、送れた分だけ消します。失敗したメモは**次に話したときに一緒に再送**されます。同じ `requestId` のままなので、実は届いていた場合も `duplicate` になり二重登録されません。
- **再送用の別フローは作らない。** Automate は再起動後、フローを最初からではなく**止まったブロックから再開**します。起動時に走る再送フローという前の設計は動きませんでした。
- **`Failure catch` は先頭に1個、上限1000。** 後続ブロックすべてを守れます。上限に達するとフローが止まる仕様なので、小さい値は危険です。

### 2-5. アシスタントとして登録する

```
設定 → アプリ → 標準のアプリ → デジタルアシスタントアプリ → Automate
設定 → 追加設定 → ボタンショートカット → 電源ボタンを長押し
Automate 左上メニュー → Settings → Run on system startup をオン
```

### 2-6. 組み終わったら確認する

1. 「テスト」と話す → バイブが鳴り、`/sdcard/Download/automate` が空になる
2. 機内モードで話す → 通知が出て、ファイルが1つ残る
3. 機内モードを切ってもう一度話す → バイブが2回鳴り、フォルダが空になる

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

止められたこと自体は Automate では検知できません。だから未送信ファイルの再送（2-4）と、
17番のバイブ（＝保存できた手応え）の両方が必要になります。

## 4. この内容の根拠

| 確認したこと | 出典 |
|---|---|
| fx で式モード／定数モードを切り替える | [Expressions](https://llamalab.com/automate/doc/expression.html) |
| 等価 `=`、論理積 `&&`、連結 `++`、辞書 `{"a":1}` | [Expressions](https://llamalab.com/automate/doc/expression.html) |
| `Spoken texts` は割り当て先の変数 | [Speech recognition](https://llamalab.com/automate/doc/block/speech_recognition.html) |
| `Failure catch` は後続の失敗を FAIL へ、上限超過で停止（既定3） | [Failure catch](https://llamalab.com/automate/doc/block/failure_catch.html) |
| `File list` の Filenames はパスの配列 | [File list](https://llamalab.com/automate/doc/block/file_list.html) |
| `For each` の出口は DO / OK | [For each](https://llamalab.com/automate/doc/block/for_each.html) |
| `File read` の出力は Text content | [File read](https://llamalab.com/automate/doc/block/file_read.html) |
| `File delete` の欄は Path / Recursive | [File delete](https://llamalab.com/automate/doc/block/file_delete.html) |
| `Timeout` 既定15秒、`Save response` 既定 Don't save | [HTTP request](https://llamalab.com/automate/doc/block/http_request.html) |
| 再起動後は止まったブロックから再開 | [FAQ](https://llamalab.com/automate/doc/faq.html) |

式エディタが受け付けない場合は、エラーメッセージと何番のどの欄かを知らせてください。

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
