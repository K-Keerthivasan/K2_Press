@echo off
title K2 Digital Media — Carousel Tool
cd /d "%~dp0"

echo Starting K2 Digital Media...
echo.

:: Check venv exists
if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found. Run setup first:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    echo   .venv\Scripts\playwright install chromium
    pause
    exit /b 1
)

:: Check Ollama is running
curl -s http://localhost:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo [WARN] Ollama not detected — start it with: ollama serve
    echo.
)

:: Activate venv
call .venv\Scripts\activate.bat

:: Open browser after 2 second delay (background)
start "" /b cmd /c "timeout /t 2 >nul && start http://localhost:8000"

:: Start FastAPI
echo K2 Press running at http://localhost:8000
echo Press Ctrl+C to stop.
echo.
uvicorn app:app --host 0.0.0.0 --port 8000 --reload

pause
