# 智能餐饮经营分析与智能采购决策系统

> 面向企业食堂/餐饮场景的「经营分析 + 智能采购」MVP：自动采购、风险人工审批、入库、库存更新与展示一条龙。

## 1. 项目背景

模拟餐饮后厨日常：库存监控 → 价格/供应商分析 → 决定是否采购 →（低风险自动 / 高风险人工审批）→ 采购执行入库 → 库存更新 → 看板展示。强调**可解释、可演示、可回归**：决策交给确定性规则，风险分析交给 LLM 解释，最终授权交给人工。

## 2. 技术架构

- **FastAPI + MySQL(async, aiomysql/SQLAlchemy) + Redis**：业务 API / 持久化 / checkpoint 与锁。
- **LangGraph** 编排采购工作流：三路确定性分析并行（Fan-out）→ 规则裁决（Fan-in）→ 分支路由。
- **Human-in-the-loop**：REVIEW 分支 `interrupt` 挂起，人工 approve/reject 后经 Redis checkpoint 从断点续跑。
- **Checkpoint Recovery**：`AsyncRedisSaver` 存档中断点；即使 checkpoint 丢失，审批也会用数据库重建 state 恢复。
- **AI Decision Snapshot**：Agent5 的结构化分析（summary/risk_level/risk_analysis/recommendation）落库 `purchase_orders.agent5_*`，MySQL 为业务事实源，checkpoint 仅作 workflow recovery。

**请准确表述（不要包装成“多个 LLM Agent 完成所有任务”）：**
> LangGraph 编排多个分析节点——其中确定性节点负责业务计算（库存/价格/供应商事实、Policy 规则），**LLM Agent 只负责风险分析与解释生成**（Agent5）。

## 3. 业务流程图

```text
采购需求
  → 三确定性分析（库存·价格·供应商，并行）
  → deterministic_policy（PURCHASE / REVIEW / NO_PURCHASE）
       PURCHASE → PO(source=AUTO) → execute_inbound_stock → COMPLETED
       REVIEW   → Agent5 → interrupt → SUSPENDED
                    ├─ approve → execute_inbound_stock → COMPLETED
                    └─ reject  → REJECTED（不入库）
       NO_PURCHASE → 不建单
  → MySQL（orders / order_items / inbound_records / inventory）
  → Dashboard（展示 🤖 自动采购 / 👤 人工审批 / Agent5 分析 / 审批状态）
```

职责边界：**Policy 决策，Agent5 解释，HITL 审批，Service/execute_inbound_stock 执行入库**。

## 4. 数据模型

- `purchase_orders`：统一订单（`source`=AUTO/MANUAL；status=RUNNING/SUSPENDED/COMPLETED/REJECTED；审批字段；Agent5 快照）
- `purchase_order_items`：订单明细（一单一食材；`UNIQUE(order_id,ingredient_id)`）
- `inbound_records`：实际入库事实快照（`UNIQUE(order_item_id)`，1:1，幂等）
- `inventory`：当前库存状态；`ingredients` / `suppliers`

MVP 约束：一个采购任务对应一个食材一张 PO；多食材聚合单属后续增强。

## 5. Agent 设计说明

- Agent1/2/3 = 确定性分析节点（计算事实，不调用 LLM）。
- Policy = 确定性规则裁判（数量/金额/风险/三态的唯一来源）。
- Agent5 = 唯一真正 LLM 节点：解释 REVIEW 风险，输出 canonical（risk_level 收口 HIGH/MEDIUM/LOW/UNKNOWN），不改决策、不建单、不入库。
- HITL = interrupt + approve/reject 的真实人工闸门。

## 6. 运行 / 测试 / Demo

- 启动：`scripts/start.sh`（Redis 容器、MySQL、Ollama 检查后起 FastAPI）。
- 测试：`python -m pytest -q`（离线）; `TEST_LIVE=1 python -m pytest -q`（真实 MySQL/Redis；Agent5 单元用 monkeypatch，Phase 5-B 用真 Ollama）。
- Demo 场景：见 [docs/demo-scenarios.md](docs/demo-scenarios.md)；架构/概念文档见 [docs/](docs/)。

## 7. 当前状态

核心业务闭环（自动采购 / 人工审批 / 入库 / Agent5 快照 / Dashboard 展示）已实现并回归通过；
剩余候选（多食材单、RBAC、只读 CRUD API、出库/库存流水等）列作后续增强，非当前 MVP 范围。
