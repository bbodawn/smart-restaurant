@echo off
REM =====================================================
REM  Smart Restaurant AI Agent Dashboard - Stop Script
REM  Kill the uvicorn process listening on port 8000
REM =====================================================

echo === Stop FastAPI service (port 8000) ===
set "done="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    if not defined done (
        set done=1
        echo [OK] Kill process PID: %%a
        taskkill /F /PID %%a >nul 2>&1
    )
)
if not defined done (
    echo [INFO] Nothing listening on 8000, service not running
)

echo.
echo Note: Redis container sr-redis-stack keeps running.
echo To also stop Redis, run: docker stop sr-redis-stack
echo.
echo Done.
