# Automate から直接入力する（Gemini を経由しない）

電源ボタン長押しの音声入力はそのままに、入力の届け先を Gemini から Automate に
差し替える手順です。Gemini の言い換えがなくなり、1分ポーリングの待ち時間も消えます。

| | Gemini 経由（現在） | Automate 直送 |
|---|---|---|
| 発話 | 「AIメモ、〜、AIメモ」 | 用件だけ |
| 原文の保持 | 言い換え・日時補完が起きることがある | 音声認識の生テキストがそのまま |
| PCが動き出すまで | 最大30秒（ポーリング待ち） | 1秒程度（取り込み時に即合図） |
| 届いた確認 | ローカルの処理完了まで分からない | 発話直後にバイブ |
| 圏外時 | 失敗しても気づけない | Automate 側で再送できる |

「AIメモ」で囲む必要がなくなるのは、あの合図が Gemini にこのフローを使わせるための
ものだからです。AI Inbox リストに入ったタスクは、マーカーがなくても
`dedicated_inbox` として信頼されます。

## 1. Apps Script を再デプロイする

`integrations/apps_script_calendar.gs` に `tasks_ingest` アクションを追加済みです。
Apps Script エディタに貼り直し、**既存デプロイを更新**してください（新規デプロイに
すると URL が変わり、`config/calendar_webhook.json` と食い違います）。

## 2. PC から先に動作確認する

スマホを触る前に、PC から同じリクエストを送って確認します。

```bash
./runtime/run-worker.sh scripts/send_test_memo.py "テスト、明日の10時に歯医者って通知して" --twice
```

`signalled=True` が出れば、取り込みと同時にローカルへ合図が飛んでいます。
`--twice` は同じ `request_id` で2回送り、重複排除が効くことを確認します。
2回目が `duplicate=True` になれば正常です。

ここが通らないうちはスマホ側に進まないでください。原因の切り分けが難しくなります。

## 3. Automate のフローを作る

必要なブロックは5つです。

```
1. Flow beginning
2. Speech recognize           →  variable: text
3. HTTP request
      Method : POST
      URL    : <config/calendar_webhook.json の endpoint_url>
      Content type: application/json
      Body   : {"secret":"<同ファイルの secret>",
                "action":"tasks_ingest",
                "text":text,
                "request_id":requestId}
4. Vibrate                    （成功時の手応え）
5. Flow end
```

- `requestId` は 2 の直前に `Variable set` で `random()` や現在時刻から作ります。
  これがあると、電波が悪くて再送になっても二重登録されません。
- HTTP request ブロックの失敗側の出口は 4 ではなく「通知を出す」に繋いでください。
  **失敗が無音だと、届かなかったことに気づけません。**
- 圏外対策をするなら、失敗側から `Wait` → HTTP request に戻す再試行ループにします。

### アシスタントとして起動させる

```
設定 → アプリ → 標準のアプリ → デジタルアシスタントアプリ → Automate
```

Automate 側では、フローの開始ブロックを「Assist request」系のトリガーにします。

## 4. Xiaomi（HyperOS / MIUI）での必須設定

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

## 5. Gemini 経由は残しておく

`tasks_ingest` は既存の経路に何も影響しません。Gemini からの投入も、1分ポーリングも
そのまま動きます。Automate が不調なときの逃げ道として、しばらく併用してください。

どちらの経路で入ったかは次で確認できます。

```bash
./runtime/run-worker.sh scripts/show_inbox.py --details
```
