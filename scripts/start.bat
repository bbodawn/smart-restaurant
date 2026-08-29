@echo off
REM =====================================================
REM  Smart Restaurant AI Agent Dashboard - Start Script
REM  Auto-start Redis (Docker) then launch FastAPI
REM =====================================================
cd /d "%~dp0.."

echo.
echo === [1/4] Start Redis container (sr-redis-stack) ===
docker inspect sr-redis-stack >nul 2>&1
if errorlevel 1 (
    echo [INFO] Container not found, creating...
    docker run -d --name sr-redis-stack -p 6379:6379 redis/redis-stack-server:latest
) else (
    docker start sr-redis-stack >nul 2>&1 && echo [OK] Redis started
)
timeout /t 2 /nobreak >nul

echo.
echo === [2/4] Check MySQL (port 3306) ===
netstat -an | findstr ":3306" | findstr "LISTENING" >nul
if errorlevel 1 (
    echo [WARN] MySQL is NOT listening on 3306. Please start your local MySQL first.
    echo        DATABASE_URL=mysql+aiomysql://root:123456@localhost:3306/restaurant
) else (
    echo [OK] MySQL is running
)

echo.
echo === [3/4] Check Ollama (port 11434, optional) ===
netstat -an | findstr ":11434" | findstr "LISTENING" >nul && echo [OK] Ollama running || echo [WARN] Ollama not running (LLM falls back to local logic)

echo.
echo === [4/4] Launch FastAPI ==="
echo Browser: http://localhost:8000
echo Press Ctrl+C in this window to stop, or run stop.bat
echo.
start "" http://localhost:8000
set "DATABASE_URL=mysql+aiomysql://root:123456@localhost:3306/restaurant"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
