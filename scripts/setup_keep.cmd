@echo off
setlocal
cd /d "%~dp0"
start "" "https://accounts.google.com/EmbeddedSetup"
echo Googleへのログイン完了後、ブラウザの開発者ツールから oauth_token を取得してください。
echo 取得した値はこの画面だけに貼り付け、チャットには送らないでください。
echo.
"C:\Users\andolabuser\.pyenv\pyenv-win\versions\3.11.9\python.exe" keep_reauthenticate.py
echo.
pause
endlocal
