#!/usr/bin/env bash
# 智能餐厅 AI Agent 控制台 - 一键启动脚本 (bash)
cd "$(dirname "$0")/.." || exit 1

echo "=== 1/4 启动 Redis (Docker sr-redis-stack) ==="
if docker inspect sr-redis-stack >/dev/null 2>&1; then
  docker start sr-redis-stack >/dev/null 2>&1 && echo "[OK] Redis 容器已启动"
else
  echo "[INFO] 创建 sr-redis-stack 容器..."
  docker run -d --name sr-redis-stack -p 6379:6379 redis/redis-stack-server:latest
fi
sleep 2

echo ""
echo "=== 2/4 检查 MySQL (3306) ==="
if netstat -an | grep ":3306" | grep -q LISTENING; then
  echo "[OK] MySQL 正在运行"
else
  echo "[WARN] MySQL 未监听 3306，请先启动本机 MySQL"
fi

echo ""
echo "=== 3/4 检查 Ollama (11434) ==="
if netstat -an | grep ":11434" | grep -q LISTENING; then
  echo "[OK] Ollama 正在运行"
else
  echo "[WARN] Ollama 未运行（LLM 分析走本地兜底逻辑）"
fi

echo ""
echo "=== 4/4 启动 FastAPI。浏览器访问 http://localhost:8000，Ctrl+C 停止 ==="
export DATABASE_URL="mysql+aiomysql://root:123456@localhost:3306/restaurant"
exec ./.venv/Scripts/python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
