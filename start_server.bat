@echo off
cd /d D:\OneDrive\CODE\PythonProject\cron_news
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
