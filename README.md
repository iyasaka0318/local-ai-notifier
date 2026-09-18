# Local AI Notifier

Google Tasksを音声入力用のAI Inboxとして利用し、ローカルのQwenで依頼を分類・実行する個人用自動化システムです。調査、日時指定リマインダー、継続リマインダー、Googleカレンダー、Web監視、起床時ブリーフィング、ntfy通知に対応しています。

入力は携帯からの音声なので、**聞き返さない**ことを設計の前提にしています。曖昧な依頼は保留せず、必ず実行できる形に倒したうえで、何をしたかを通知で報告し、通知のボタン1つで取り消せるようにしています。

## ディレクトリ構成

| パス | 内容 |
|---|---|
| `src/` | 実行ロジック、ワーカー、外部サービス用クライアント |
| `tests/` | 自動テスト |
| `scripts/` | 初期設定、認証、DB確認などの管理ツール |
| `config/` | 認証情報と端末固有設定（Git管理外） |
| `data/` | 稼働中のSQLite DB（Git管理外） |
| `backups/` | DBバックアップ（Git管理外） |
| `logs/` | 実行ログ（Git管理外） |
| `runtime/` | 多重起動防止ロック（Git管理外） |
| `docs/` | セットアップ資料とGemini向け指示 |
| `integrations/` | Google Apps Scriptなど外部側のコード |

ルートの `.cmd` / `.vbs` はWindowsタスクスケジューラとの互換エントリーポイントです。業務ロジックはすべて `src/` にあります。

## 処理の流れ

1. `tasks_event_listener.py` がGoogle Tasksの更新信号を受信
2. `inbox_cycle.py` が各ワーカーを順番に起動
3. `watch_keep.py` が入力を整形し、Qwenで意図を分類
4. 各ワーカーが調査・通知・カレンダー登録・監視などを実行
5. 状態を `data/keep_state.db` に保存し、必要に応じてntfyへ通知

## 主な実行方法

```powershell
# AI Inboxを1サイクル処理
python src/inbox_cycle.py

# イベントリスナー
.\run_tasks_listener.cmd

# 全テスト
$env:PYTHONPATH = "$PWD\src"
python -m unittest discover -s tests -v

# DB状態の確認
python scripts/show_inbox.py --details
```

通常運用では環境変数 `NTFY_TOPIC` と、`config/` 内のローカル認証設定が必要です。QwenはOllamaの `qwen3:14b` を `http://localhost:11434` で利用します。

構造化JSONを高速に生成するため、Ollamaのthinkingは既定で無効です。精度比較などでthinkingを戻す場合は `AI_OLLAMA_THINK=true` を設定してください。稼働中のSQLite DBは次のコマンドで整合性を保ったままバックアップできます。

```bash
python scripts/backup_db.py
```

保存先は `backups/`、保持数は既定30件です。`AI_EXTERNAL_BACKUP_DIR` を設定すると外部ストレージにも同時保存します。

## 音声入力向けの設計

| 仕組み | 内容 |
|---|---|
| 決め打ち実行 | 時刻が読めないリマインダーは継続リマインドへ、日付が読めない予定はTODOへ、判断できない入力はメモへ倒します。`waiting_information` で放置しません（`src/intent_fallback.py`） |
| 実行報告 | 1メモにつき1通、登録した内容と倒した理由を通知します（`src/report_notifier.py`） |
| 複数依頼の分割 | 「明日9時に歯医者、あと牛乳買うのリマインド、第74回の監視も」のような1メモを、依頼ごとに分けて実行します |
| 訂正 | 「さっきのリマインダー9時じゃなくて10時」で直近の登録を変更・取消できます。直近の操作履歴を分類時にモデルへ渡しています |
| ワンタップ取消 | 通知の「取り消し」ボタンがntfyの制御トピックへ投稿し、`src/control_listener.py` が受けて元に戻します。カレンダーはGoogle側の予定も削除します |
| まとめ通知 | 「買い物」などのグループに属する継続リマインドは、1通にまとめて届きます |

制御トピックは既定で `{NTFY_TOPIC}-control` です。`NTFY_CONTROL_TOPIC` で変更できます。

入力はGemini経由のほか、Automateなどのスマホ自動化アプリから `tasks_ingest` で直接投入できます。
Geminiの言い換えが入らず、取り込み時点でローカルへ合図を送るのでポーリング待ちも発生しません。
手順は `docs/AUTOMATE_SETUP.md` を参照してください。
カレンダー予定の取り消しには、`integrations/apps_script_calendar.gs` の再デプロイが必要です（`calendar_delete` アクションを追加済み）。

## 安全設計

- 処理済み入力と生成済みKeepメモをSQLiteで追跡し、二重処理を防止
- 入力元タスクは下流処理の成功後にのみ完了扱い
- リマインダーやWeb監視の取消は状態変更で保持し、即時削除しない
- 同一の処理失敗通知は一定時間抑制し、ntfy障害時の通知ループを防止
- `config/`、`data/`、`backups/`、`logs/`、`runtime/` はGit管理外
- 通知は状態を確定させてから送り、送信に失敗した場合だけ元の状態へ戻す
- 中断した処理は次回起動時に回収する（`requeue_stale_running_jobs`）
- 1メモが複数の依頼を含む場合、すべて完了するまで入力元タスクを完了にしない
