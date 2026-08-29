@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM =====================================================
REM  智能餐厅 AI Agent 控制台 - 一键启动脚本
REM  自动启动 Redis (Docker) 并拉起 FastAPI 服务
REM =====================================================
cd /d "%~dp0.."

echo.
echo === 1/4 检查并启动 Redis (Docker sr-redis-stack) ===
docker inspect sr-redis-stack >nul 2>&1
if errorlevel 1 (
    echo [INFO] 未找到 sr-redis-stack 容器，正在创建...
    docker run -d --name sr-redis-stack -p 6379:6379 redis/redis-stack-server:latest
) else (
    docker start sr-redis-stack >nul 2>&1 && echo [OK] Redis 容器已启动
)
timeout /t 2 /nobreak >nul

echo.
echo === 2/4 检查 MySQL (端口 3306) ===
netstat -an | findstr ":3306" | findstr "LISTENING" >nul
if errorlevel 1 (
    echo [WARN] MySQL 未在 3306 端口监听！请先启动本机 MySQL 服务。
    echo        数据链接为 mysql+aiomysql://root:123456@localhost:3306/restaurant
    echo        请确认该账号和密码，或用环境变量 DATABASE_URL 覆盖。
) else (
    echo [OK] MySQL 正在运行
)

echo.
echo === 3/4 检查 Ollama (端口 11434, 仅提示) ===
netstat -an | findstr ":11434" | findstr "LISTENING" >nul && echo [OK] Ollama 正在运行 || echo [WARN] Ollama 未运行（LLM 分析会走本地兜底逻辑，不影响核心流程）

echo.
echo === 4/4 启动 FastAPI 服务 ===
echo 浏览器访问: http://localhost:8000
echo 按 Ctrl+C 停止服务（或运行 stop.bat）
echo.
start "" http://localhost:8000
set "DATABASE_URL=mysql+aiomysql://root:123456@localhost:3306/restaurant"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000

endlocal
