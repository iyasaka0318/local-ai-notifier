# Google Calendar 初回設定

Google Calendarへ予定を登録するには、最初の1回だけGoogleのOAuth認証が必要です。

1. [Google Calendar API](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com) を有効にします。
2. Google Auth Platformの「ブランド」「対象」「データアクセス」を設定します。テスト中の場合は、使用するGoogleアカウントをテストユーザーに追加します。
3. Google Auth Platformの「クライアント」から、アプリの種類を「デスクトップアプリ」としてOAuthクライアントを作成します。
4. ダウンロードしたJSONを次の名前で保存します。

   `C:\Users\andolabuser\ai_notifier\config\google_calendar_client_secret.json`

5. `scripts\setup_calendar.cmd` を実行し、ブラウザで使用するGoogleアカウントを認証します。

認証後は `config\google_calendar_token.json` が生成されます。これらのJSONは認証情報のため、他人に送信しないでください。

初回認証後は、Tasks更新時または保険の定期確認時に `src\calendar_worker.py` が自動実行されます。

Google公式参考:

- [Google Calendar API Python quickstart](https://developers.google.com/workspace/calendar/api/quickstart/python)
- [OAuth 2.0 for Desktop Apps](https://developers.google.com/identity/protocols/oauth2/native-app)
