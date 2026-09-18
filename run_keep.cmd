@echo off
cd /d C:\Users\andolabuser\ai_notifier
set "PYTHONDONTWRITEBYTECODE=1"
set "worker_exit=0"
if not exist logs mkdir logs
call python src\watch_keep.py >> logs\keep_worker.log 2>&1
if errorlevel 1 set "worker_exit=1"
call python src\research_worker.py >> logs\keep_worker.log 2>&1
if errorlevel 1 set "worker_exit=1"
call python src\calendar_worker.py >> logs\keep_worker.log 2>&1
if errorlevel 1 set "worker_exit=1"
call python src\wake_worker.py >> logs\keep_worker.log 2>&1
if errorlevel 1 set "worker_exit=1"
call python src\reminder_worker.py >> logs\keep_worker.log 2>&1
if errorlevel 1 set "worker_exit=1"
exit /b %worker_exit%
