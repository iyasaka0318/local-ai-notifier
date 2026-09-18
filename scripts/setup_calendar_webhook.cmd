@echo off
setlocal
cd /d "%~dp0"
"C:\Users\andolabuser\.pyenv\pyenv-win\versions\3.11.9\python.exe" calendar_webhook_setup.py
echo.
pause
endlocal
