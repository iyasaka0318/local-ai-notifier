@echo off
cd /d C:\Users\andolabuser\ai_notifier
set "PYTHONDONTWRITEBYTECODE=1"
if not exist logs mkdir logs
call python src\tasks_event_listener.py >> logs\tasks_event_listener.log 2>&1
