@echo off
cd /d C:\Users\user\ai\h3-studio\backend
echo ==== boot at %date% %time% ==== >> C:\Users\user\ai\h3-studio\backend\backend_boot.log
C:\Users\user\ai\h3-studio\backend\venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8790 >> C:\Users\user\ai\h3-studio\backend\backend_boot.log 2>&1
