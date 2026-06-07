@echo off
setlocal
title K2 Digital Media - Initial Setup
cd /d "%~dp0"

echo K2 Digital Media initial setup
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH.
    echo Install Python 3.11+ or 3.12+, then run this setup again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 goto fail
) else (
    echo Virtual environment already exists.
)

echo Upgrading pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto fail

echo Installing Python dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail

echo Installing Playwright Chromium...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto fail

if not exist ".env" (
    echo Creating .env placeholder...
    > ".env" echo PEXELS_API_KEY=
    >> ".env" echo UNSPLASH_API_KEY=
    echo Add your API keys to .env before image fetching.
) else (
    echo .env already exists.
)

echo.
echo Checking Docker...
where docker >nul 2>&1
if errorlevel 1 (
    echo [WARN] Docker was not found on PATH. Local setup is complete.
) else (
    docker compose version >nul 2>&1
    if errorlevel 1 (
        echo [WARN] Docker is installed, but 'docker compose' is not available.
    ) else (
        echo Docker Compose is available.
        choice /C YN /N /M "Build the Docker image now? [Y/N] "
        if errorlevel 2 goto docker_done
        docker compose build
        if errorlevel 1 goto fail
        :docker_done
    )
)

echo.
echo Setup complete.
echo.
echo Local run:
echo   run.bat
echo.
echo Docker run:
echo   docker compose up --build
echo.
echo If you use Ollama, make sure it is running on Windows:
echo   ollama serve
echo   ollama pull qwen3:8b
echo.
pause
exit /b 0

:fail
echo.
echo [ERROR] Setup failed. Check the message above.
pause
exit /b 1
