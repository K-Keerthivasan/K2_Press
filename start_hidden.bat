@echo off
rem Launch the K2 Press host server (uvicorn) and log to k2_server.log.
rem Called hidden by the Startup launcher (K2Press.vbs). Manual use is fine too.
cd /d "%~dp0"
.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000 >> k2_server.log 2>&1
