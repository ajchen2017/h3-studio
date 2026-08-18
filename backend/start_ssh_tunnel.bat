@echo off
cd /d C:\Users\user\ai\h3-studio\backend
echo ==== boot at %date% %time% ==== >> C:\Users\user\ai\h3-studio\backend\ssh_tunnel_boot.log
C:\Users\user\ai\h3-studio\backend\venv\Scripts\python.exe ssh_tunnel_watchdog.py >> C:\Users\user\ai\h3-studio\backend\ssh_tunnel_boot.log 2>&1
