# 测试套件说明（证据链纪律）

## 证据标签（报告必须区分，绝不混用）

| 标签 | 含义 |
| --- | --- |
| `PASS` | 用例运行且断言全过 |
| `MOCK TEST` | 依赖对象/checkpoint 为替身（如 InMemorySaver / 假 graph / monkeypatch 掉 LLM），**非真实基础设施** |
| `LIVE E2E` | 走真实 MySQL + Redis 的端到端验证 |
| `NOT RUN` | 因环境/未实现等原因**未运行**，绝不标 PASS |
| 静态检查 | 对源码做不变量断言（不运行应用） |

## 目录

```
tests/
├── unit/        # 无任何外部依赖：Policy / 分析节点 / Agent5(monkeypatch) /
│                #   Graph 拓扑 / HITL（后两者用 InMemorySaver → MOCK TEST）
├── contract/    # 服务端防御降级契约 + 前端展示契约（静态检查）
└── live/        # 真实 MySQL + Redis（LIVE E2E）；默认 skip → 报告口径 NOT RUN
```

## 运行

```bash
# 1) 只跑离线套件（unit + contract + live 自动 skip）：无需任何服务
./.venv/Scripts/python.exe -m pytest -q

# 2) 跑真实基础设施 E2E（需要 MySQL + Redis；默认连接本机 root/123456 与 restaurant_it）
TEST_LIVE=1 ./.venv/Scripts/python.exe -m pytest tests/live -v
```

## LIVE 隔离约定（重要）

- 使用独立测试库 `restaurant_it`（每次会话从 `sql/init.sql` 重建，清空默认种子）。
  **绝不触碰**开发库 `restaurant` 或既有 `restaurant_test`。
- Redis 用 db 0（LangGraph checkpoint 的 redisvl 索引要求 db 0），**不 flush**，
  靠 uuid thread_id 隔离，避免清空应用开发数据。
- 环境变量：
  - `TEST_MYSQL_URL`（默认 `mysql+aiomysql://root:123456@127.0.0.1:3306/restaurant_it`）
  - `TEST_REDIS_URL`（默认 `redis://127.0.0.1:6379/0`）

## 设计说明

- `pytest.ini`：`asyncio_mode=auto` + **session 级事件循环**。
  原因：`app.core.redis.redis_client` 是模块级单例，首次使用即绑定到当时的事件循环；
  若每用例单独 loop，跨用例复用该单例会报 `Event loop is closed`。
- Agent5 的真实 LLM 未在自动化里调用（避免模型/耗时波动）；其
  schema / fallback / 不改 policy 均用 monkeypatch 验证为 MOCK TEST。
