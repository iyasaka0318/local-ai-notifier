@echo off
cd /d C:\Users\andolabuser\ai_notifier
set "PYTHONDONTWRITEBYTECODE=1"
if not exist logs mkdir logs
python src\monitor_worker.py >> logs\monitor_worker.log 2>&1
