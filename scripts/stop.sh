#!/usr/bin/env bash
# 智能餐厅 AI Agent 控制台 - 一键关闭脚本 (bash, Windows)
echo "=== 关闭 FastAPI 服务 (8000) ==="
pid=$(netstat -ano | grep ":8000" | grep "LISTENING" | awk '{print $NF}' | head -1)
if [ -n "$pid" ]; then
  taskkill //F //PID "$pid" >/dev/null 2>&1 && echo "[OK] 已结束进程 PID: $pid" || echo "[WARN] 结束进程 PID $pid 失败"
else
  echo "[INFO] 8000 端口无监听，服务可能未运行"
fi

echo ""
echo "提示：Redis 容器 sr-redis-stack 保持运行；如需停止：docker stop sr-redis-stack"
