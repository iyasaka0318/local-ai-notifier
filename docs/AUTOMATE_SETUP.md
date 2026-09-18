# Automate から直接入力する（Gemini を経由しない）

電源ボタン長押しの音声入力はそのままに、入力の届け先を Gemini から Automate に
差し替える手順です。Gemini の言い換えがなくなり、1分ポーリングの待ち時間も消えます。

| | Gemini 経由（現在） | Automate 直送 |
|---|---|---|
| 発話 | 「AIメモ、〜、AIメモ」 | 用件だけ |
| 原文の保持 | 言い換え・日時補完が起きることがある | 音声認識の生テキストがそのまま |
| PCが動き出すまで | ポーリング待ち（経路が正常なら数十秒） | 1秒程度（スマホから直接合図） |
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

Apps Scriptは**認証失敗も例外も、HTTP 200のJSONで返します**。応答が返ってきた
ことは保存成功を意味しません。レスポンス本文を確認する分岐が要ります。

```
 1. Flow beginning
 2. Variable set      requestId = 端末の時刻＋乱数
 3. Speech recognize  →  text
 4. Variable set      端末に {requestId, text} を保存（未送信キュー）

 5. HTTP request  ①取り込み
       Method        : POST
       URL           : <config/calendar_webhook.json の endpoint_url>
       Content type  : application/json
       Body          : {"secret":"<同ファイルの secret>",
                        "action":"tasks_ingest",
                        "text":text,
                        "request_id":requestId}
       Response body : ★保存を有効にする（既定は無効）
       Read timeout  : ★60秒にする（既定は15秒）

 6. 判定  status == 200 かつ JSONの ok == true かつ task_id がある？
       いいえ → 通知「保存できませんでした」＋未送信キューに残す＋終了
       はい   → 次へ（この時点で保存は確定）

 7. Vibrate           保存できた手応え
 8. 未送信キューから削除

 9. HTTP request  ②起動合図
       Method        : POST
       URL           : <config/tasks_event.json の signal_url>
       Content type  : text/plain
       Body          : tasks_changed
       失敗しても再送しない（②だけの失敗は遅延であって損失ではない）

10. Flow end
```

### 実装上の注意

- **既定タイムアウトは15秒です。** Apps Scriptはトリガーと重なると数十秒かかる
  ことがあるので、60秒へ伸ばしてください。報告された「15秒かかった」はこの既定値
  とも一致します。
- **①と②で再送処理を分けてください。** ①の失敗は「保存できていないかもしれない」、
  ②の失敗は「保存済みだが通知が遅れる」で、取るべき行動が違います。
- **再送時は必ず同じ `requestId` を使い回してください。** 新しく採番すると二重登録
  されます。タイムアウトは「失敗」ではなく「結果が分からない」状態で、サーバー側では
  成功していることがあります。
- **送信前に端末へ本文と `requestId` を保存してください。** HyperOSにアプリを止め
  られても、次回起動時に未送信分を再送できます。

### なぜ合図を自分で送るのか

Apps Script に合図を送らせることもできますが、**Googleの送信元から ntfy.sh への
接続は断続的に数十秒ハングします**。その待ち時間がそのまま①の応答時間になり、
呼び出し側がタイムアウトします（実測で30秒・60秒とも突破しました）。

スマホからもPCからも ntfy へは1秒未満で届くので、合図は自分で送るのが速く確実です。
①が失敗しても②は送る必要はありません。逆に②だけ失敗しても、タスクは保存済みなので
定期ポーリングが最大30秒で拾います。**どちらの失敗もデータは失われません。**

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
