@echo off
chcp 65001 >nul
setlocal

REM =====================================================
REM  智能餐厅 AI Agent 控制台 - 一键关闭脚本
REM  关闭占用 8000 端口的 uvicorn 服务
REM =====================================================

echo === 关闭 FastAPI 服务 (端口 8000) ===
set "done="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    if not defined done (
        set done=1
        echo [OK] 结束进程 PID: %%a
        taskkill /F /PID %%a >nul 2>&1
    )
)
if not defined done (
    echo [INFO] 8000 端口无监听，服务可能未运行
)

echo.
echo === 提示：Redis 容器 sr-redis-stack 将保持运行 ===
echo 如需一并停止 Redis，请执行:  docker stop sr-redis-stack
echo.
echo 完成。

endlocal
