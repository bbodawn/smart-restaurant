# Architecture Audit

> 智慧餐饮经营分析与智能采购决策系统 —— 只读架构审查
> 审查模式：目标是把串行 Agent 流程重构为「并行分析 + 最终决策」，评审该方向在当前真实代码下是否成立、以及应做什么、不该做什么。

Audit Mode: READ ONLY

---

## 1. Executive Summary

当前系统是 **「串行一条龙式 LangGraph 采购链」**：一次 `graph.ainvoke` 从建单一路顺序走完
`demand → inventory → supplier → risk(可 interrupt 挂起) → purchase`，由 **API 层在图外**负责建单、落库、以及（审批后）物理入库 `execute_inbound_stock`。

真实情况与“并行 Multi-Agent”现状：**图内没有任何真正的并行分支**，且大量“Agent”只是**纯确定性 Python 函数**，真正调用 LLM（Ollama `qwen2.5:7b`）的只有：
- `demand_agent`（需求分析说明，`demand_reasoning`）
- `risk_agent`（风控告警“补充分析”段）

即：**当前 5 个图节点里，只有“解释性文案”是 LLM 生成的；决策本身全部由确定性代码/规则完成**（库存够否、quantity、total_amount、价格偏离、风险触发、whether need purchase）。

结论（先给，后面逐条给证据）：
1. 把库存/价格/供应商拆成“并行 Agent”并让**每个都真去调用 LLM**，会引入：额外延迟、非线性输出、非幂等、难以回归；而它们要做的其实是**确定性计算 + 本地规则**，不是 LLM 推理。
2. “并行”**能成真**的部分是：库存健康度、价格偏离、供应商/金额这几个**事实读取与判定彼此无数据依赖**，可以并行。但它们应当是 **确定性 Calculator/工具节点**，并行执行并汇总结论给“一个决策节点”与“风控/HITL”，而不是 3 个各自吐字的 LLM Agent。
3. 推荐的最小改动范式：**“事实分析(可并行, 确定性) → 规则决策(policy) →（必要时 LLM 出解释文案）→ 风险分级 → HITL/自动”**。这与用户设想方向一致，但把“该LLM、不该LLM”的边界划干净。

---

## 2. Current Architecture

### CURRENT ARCHITECTURE（真实接线，`app/graph/workflow.py`）

```
 START
   │
 demand_agent        （图节点·确定性算 predicted_demand=daily_sales*3；LLM 只生成一句 reason 文案）
   │
 inventory_agent     （图节点·确定性：库存>=预测 且 >安全线 → NO_PURCHASE；否则求补货缺口）
   │
 supplier_agent      （图节点·纯确定性：total_amount = quantity × supplier_price）
   │
 risk_agent          （图节点·确定性规则判隐患；命中 → interrupt(HITL SUSPENDED)；否则 APPROVED）
   │  （LLM 仅在该节点出错时或命中时给 “补充分析” 说明，不影响决策）
   │
 purchase_agent      （图节点·纯状态映射：APPROVED→PURCHASE_CREATED / REJECTED→REJECTED / 等）
   │
 END
```
- 另有一个 **`extension_agent` 占位节点**：已 `add_node` 但**未 add_edge**，不参与主流程（预留扩展）。
- 真正的“采购/建单/入库/审批”都在**图外的 API/Service**，不在 Graph 内（见 §4）。

---

## 3. Current Agent Responsibilities（逐个核对真实实现）

| 名称（文件/行） | 真实职责 | 是 Agent？ | 真调用 LLM？ | 判定/计算本质 |
| --- | --- | --- | --- | --- |
| `demand_agent` (`graph/nodes.py`) | 读 `daily_sales`，predicted = daily×3；用 LLM(gpt=get_llm()) 生成一句 `demand_reasoning`（失败时有本地兜底） | 名义 Agent | ✅ 是（仅文案） | 数量是确定性；文案是 LLM |
| `inventory_agent` (`nodes.py`) | 依 current/safety/predicted 判定 NO_PURCHASE 或补货缺口 & 提高到安全线 | 名义 Agent | ❌ | **纯确定性，无需 LLM** |
| `supplier_agent` (`nodes.py`) | 依 quantity×supplier_price 算 total_amount | 名义 Agent，但名字有误导性：它**不做供应商选择/比价** | ❌ | **纯确定性算术** |
| `risk_agent` (`nodes.py`) | 价格偏离>20% / 金额>5000 / 库存<=安全线 → interrupt(HITL)；生成结构化告警 `risk_analysis_report`，并可能让 LLM 追加“补充分析” | 名义 Agent | ✅（仅补充文本） | 决策=确定性规则；报告=确定性+LLM 追加 |
| `purchase_agent` (`nodes.py`) | 把 `status` 归一（APPROVED→PURCHASE_CREATED…） | 名义 Agent | ❌ | **纯状态映射** |
| `extension_agent_node` (`nodes.py`) | 写占位字符串，未接边 | 占位 | ❌ | 无作用 |

**重要**：`app/agents/` **目录不存在**——Agent 都在 `app/graph/nodes.py`。命名上“Agent 1=库存 / Agent 3=供应商 / Agent 5=决策”等概念在当前文件里**并不存在**，是用户新规划，尚未落地。

---

## 4. Current Data Flow（数据流 + 建单/入库在图中还是在 API）

采购**入口有两处**，都在图外：

1. 人工/手动：`POST /api/v1/purchase`(`api/purchase.py`)
2. 自动巡检：`services/auto_procurement.scan_and_trigger_procurement`（低库存 `current_stock<=safety_stock`，去重锁防重）

两条入口都做同一件事：
```
拼 PurchaseState(含 order_id/order_no/thread_id + 已由 DB LEFT-JOIN 取好的 单食材单供应商快照)
→ INSERT purchase_orders(status=RUNNING)
→ graph.ainvoke(state, config={thread_id})
→ 若返回 __interrupt__：把订单置 SUSPENDED + 写 purchase_order_items + commit
→ 若没 interrupt：读 result.status，回写订单；quantity>0 走 execute_inbound_stock 直接物理入库
```

**审批恢复**：`POST /purchase/{id}/approve`(api/purchase.py `approve_purchase_order`)：
```
acquire_approval_lock(Redis) → resume(Command(resume={approved:true}, update=resume_state))
→ execute_inbound_stock(db, order_id)  // 库存 current_stock+=qty，单置 COMPLETED+completed_at
→ 释放锁
```
另有一个旧版空闲路由 `/approve/{order_id}`（非“补库”语义），及 `services/auto_approval.auto_approve_suspended_orders`（挂起超 3 虚拟天自动放行）。

**物理入库是全系统唯一真正“改库存”的动作**，API/服务复用 `services/inventory.execute_inbound_stock`（仓库强调的“COMPLETED⟺入库强绑定”）。

**时间加速**：`POST /api/v1/test/advance-day` → `simulate_days_passing` =（推进虚拟钟）→（扣各类库存 daily_sales×days）→（自动放行超时挂单 / 低库存巡检触发采购）。

---

## 5. Current Dependency Graph（真实依赖）

图内（串联，每节点依赖前一节点写出的字段）：
```
demand_:     写出 predicted_demand, demand_reasoning        （供 inventory 用 predicted）
inventory_:   读(current,safety,predicted)； 写 quantity,status
supplier_:    读(quantity,supplier_price)；  写 total_amount
risk_:        读(total_amount,status,supplier_price,historical_price,current_stock,safety_stock)
             写 price_deviation,risk_reason/risk_analysis_report,approved,status
purchase_:    读 status； 写 归一 status
```
图外 API→service 依赖：
- `api/purchase.py` → `core/idempotency`、`core/lock`、`services/inventory.execute_inbound_stock`、DB
- `api/test_time.py` → `services/time_simulation` → `core/clock`
- `time_simulation` → `services/auto_approval`、`services/auto_procurement` → `services/inventory`
- `main.py` lifespan → `core/redis`、`graph.workflow.build_graph(AsyncRedisSaver)`
- 所有 `.py`（除 graph/state）→ `core/db`

---

## 6. Deterministic Logic vs LLM Logic（关键裁决素材）

**明确应属“确定性 Python 代码（不能交给 LLM）”**：
- `predicted_demand = daily_sales*3`（`demand_agent`）
- 库存是否需补 / 补货缺口 = `max(predicted-current, 0)`，并抬到安全线（`inventory_agent`）
- `total_amount = quantity*prices`（`supplier_agent`）
- `price_deviation = |cur-hist|/hist`；是否 `>0.20`；金额是否 `>5000`；低库存 `<=safety`（`risk_agent` 判定）
- “该不该买 / 买多少 / 是否要我审批 / 是否 NO_PURCHASE / REJECTED / PURCHASE_CREATED”
- 建单、幂等键、状态机归一、并发锁、入库补库

**真正适合 LLM 的（当前也这么用）**：
- 一段**解释性质**的 `demand_reasoning`（给经营看板/审计读）
- 一段**风控自然语言“补充分析”**（在规则已判定的告警文本后追加 human-pausible 说明）

**结论**：用户设想的 Agent1/Agent2/Agent3 各自“能支撑几天、价格是否异常、该不该现在买”，**除了“为什么这么判断”的说辞外，几乎全部是确定性规则**。因此把决策数字交给 3 个 LLM Agent 是不必要且增加不确定性的。

---

## 7. Parallelization Analysis

### 依赖关系表（用户列出的 Target 假设节点 vs 当前代码）

| 节点 | 依赖谁（数据） | 读什么（真实字段） | 输出 | 是否可并行 |
| --- | --- | --- | --- | --- |
| Agent1 库存 | 无 | current_stock, safety_stock, daily_sales, predicted_demand | 健康度/需补缺口 `quantity`, `status` | ✅ 可与 Agent2/Agent3 并行（输入都是初始快照，不依赖他人） |
| Agent2 价格 | 无（真实代码里它只依赖初始快照 supplier_price/historical_price；**并不真正需要 Agent1 的 output**） | supplier_price, historical_price | price_deviation, 是否 >20% | ✅ 可与 Agent1/Agent3 并行；**不依赖 Agent1** |
| Agent3 供应商 | 无（代码无“选供应商”逻辑；只算 total= quantity×price，quantity 才依赖库存） | 若只算金额：quantity(来自库存)；若做供应商选择：DB 需查询，当前没查 | (若“算金额”) total_amount | ⚠ **是伪并行**：它那唯一的“quantity”来自库存分析。若目标是“供应商比价/择优”，需额外 DB 查询（当前无字段/无路径） |
| Calculator | 聚合 Agent1+2+3 结论 | 各分析结论 | 汇总“该不该买/买多少/风险分” | — |
| Agent5 决策 | Calculator / 规则 | 聚合结果 | should_purchase/purchase_quantity/risk_level | 一般是顺序在 Calculator 后 |
| HITL | 决策风险 | 决策结论 | SUSPENDED / Auto | 决策后 |
| Purchase | HITL/自动 | order | 入库补库 | 最后 |

### 问题1：Agent1 与 Agent2 是否有真实数据依赖？
**没有**——消费者一方的 price_deviation/total 与供应商数据都来自**初始快照**（`_load_ingredient_bundle` / `create_purchase` 里 DB 一次性 JOIN 已取好），`inventory_agent` 只向 state 写 `quantity/status`，`risk_agent` 需读 `total_amount`（由 supplier_agent 按 quantity 算出）——**这一环** quantity 确实来自库存。但“价格本身是否异常”并不需要库存结论。所以：
- Agent1、Agent2 的**读取与判定彼此无依赖** → 可并行；
- Agent3 若只算金额则依赖 qty（顺序于库存后）；若做“比价择商”需新数据源。

### 问题2：采购数量到底谁算？
推荐 **B（独立确定性 Calculator）**（或直接保留 `inventory_agent` 的确定性求值作为 Calculator）。
理由：
- 数量 = `predicted - current`（且抬到 safety），是**纯算术**，与“该买与否”同属规则，**不该由 LLM 或一个汇总 Agent 算**（避免波动+不可测）。
- 若放 Agent1(库存) 算，等于让“分析节点”同时做“决策值”，不利于把“并行分析”与“决策”解耦；当前代码其实已经近似这样——`inventory_agent` 在决定差异同时也给了 quantity。为对齐“并行分析+决策”范式，建议把 `predicted/quantity 计算`抽成一个纯 python Calculator，`inventory/price/supplier` 三个分析节点**只标注状态/理由**，计算集中一处。

### 问题3：哪些是 Deterministic / 哪些 LLM
见 §6。一句话：决策数值=确定性；*解释性文案*=LLM（可并行多个候选文案生成，但那不是业务决策）。

---

## 8. Proposed Multi-Agent Architecture（建议，非结论）

### PROPOSED ARCHITECTURE

```
                 START
                   │
          load purchase context (建单&取快照, 现有 API 不变)
                   │
        ┌──────────┴──────────┐
        │  并行 事实分析(纯确定性/工具)     │
   ┌────┴───┐ ┌────┴────┐ ┌─────┴────┐
   Inventory │  Price   │  Supplier  │
   (状态comfy)│(偏差/异常)│ (金额/是否可选)
   └────┬───┘ └────┬────┘ └─────┬────┘
        └──────────┼────────────┘
                   ↓
        Deterministic Calculator   (quantity / should_purchase 的硬规则)
                   ↓
           Policy/ Decision node    (低风险→auto; 命中风险→走 HITL)
                   ↓
           HITL(interrupt SUSPENDED) / Auto-purchase(直接 execute_inbound)
                   ↓
               Purchase 落库入库 (图外 service 维持现状)
                   ↓
                 END
```
说明：
- “并行”能给的是：Inventory / Price / Supplier 三个**只读、确定性、互不读写**的分析（并成一个 Layer，LangGraph 的 fan-out→fan-in，或省略成单个“聚合 context + Calculator”）。
- 只有需要“给经营看的解释/风控报告”时，让 **LLM only 用于文案**，且放在决策之后/附生，不作为决策唯一来源。
- 若想保留“用户提的 Agent1=库存 / Agent2=价格 / Agent3=供应商 / Agent5=决策”的形象，可行法 = 三个**确定性分析工具** + 一个 **决策节点**，别让每个工具去 `ainvoke` LLM。

---

## 9. Proposed State Design（在不动 DB/Frontend 前提，最小 state 调整示意）

当前 `PurchaseState`(total=False) 已承载全部字段；为“并行分析→决策”建议新增**分析结果容器字段**（都可由 code 填充）：
```python
analysis_inventory: Optional[dict]   # {health, days_cover, needs_refill, reason}
analysis_price:     Optional[dict]   # {price_deviation_raw, is_abnormal}
analysis_supplier:  Optional[dict]   # {supplier_id, total_amount, has_choice}
decision:           Optional[dict]   # {should_purchase, quantity, risk_level, need_human_approval}
```
- 现有 `quantity / total_amount / price_deviation / risk_analysis_report / approved / status` 等字段可保留复用，避免下游 API 改渲染。
- **不建议**改原有“图外建单/入库”约定；Graph 只负责“算→判→interrupt”，落库留在 service。

---

## 10. HITL Position Analysis

- 当前 HITL 由 `risk_agent` 在“命中任一规则(>20% / >5000 / 低库存)”时 `interrupt(...)`，API 把订单标 SUSPENDED，前端待办中心处理；超时由 `auto_approval` 自动放行。
- 在目标架构里，HITL 应放在**决策/风险分级之后**（决定才清楚需要人工），当前其实已自然满足（risk_agent 就是决策点+interrupt）。
- 建议把 HITL 视为**决策层的一个出口**而非某个裸 agent；延续 LangGraph `interrupt` 语义，Rediser checkpointer 复现审批即可，无需重做。

---

## 11. Purchase Quantity Responsibility

推荐 B。原因见 §7-问题2。真实代码目前已隐约是“库存 agent 算 quantity”，虽不理想但行为确定；向“Calculator”收敛是更稳做法。不要交给 D(LLM) 或 C(汇总 Agent)。

---

## 12. File-Level Impact Analysis（改动会触碰哪些文件）

**若真正落地“并行分析+决策”的最小重构，会触碰文件（估算）：**
- `app/graph/state.py`（加分析结果字段）
- `app/graph/workflow.py`（节点接线/并行 fan，若做真并行）
- `app/graph/nodes.py`（拆/重组节点），或新增 `app/graph/analyzers.py`（纯确定性工具）+ `app/graph/calculator.py` + 仅保留 `reasoning` LLM 调用
- `app/api/purchase.py`、`app/services/auto_procurement.py` 只读调用图不变（若 state 结构基本不变），基本**无需改**，除非字段名改动
- `app/graph/dashboard.py` 等仅消费 order status，**不感知 Graph**
- **database schema / 前端 / services / core 基建 **不必动

---

## 13. Regression Risk Analysis（按用户列表逐项）

| 功能 | 当前实现 | 是否依赖当前 Graph | 重构风险 | 保护方案 |
|---|---|---|---|---|
| 1. 前端页面 | SPA（index.html+本地Tailwind），读 REST，不碰 graph 内部 | 否 | 低（若 state 字段名不变/API 不变） | 不改 API response 形状、不改路由/字段名 |
| 2. 食材管理 | api/ingredients(DELETE/PUT/POST)直接 DB，不走 graph | 否 | 低 | 不动 ingredients.py |
| 3. 数据库查询 | services/api 各自 SQL；不读 graph | 否 | 低 | 不动 schema 及 SQL |
| 4. 虚拟时间 | core/clock + test_time | 否（仅触发 simulate→services→graph 入口之一） | 中（若把 graph 变更打断 auto 触发时序） | 保持 `scan_and_trigger_procurement` 的图入口 & `auto_approval` 超时逻辑不变 |
| 5. 库存自动扣减 | services/time_simulation 扣库存（DB），不依赖 graph 节点 | 否 | 低 | 不动 time_simulation 扣减 |
| 6. 自动采购触发 | services/auto_procurement scan 先建单再 graph.ainvoke | **是**（调用图） | 中 | 保持 graph.ainvoke 契约(返回 __interrupt__/status)，status 语义别变 |
| 7. 采购订单 | 图外 service 写 purchase_orders/items | 半依赖（graph 产出的 status） | 中 | 保留 RUNNING/PURCHASE_CREATED/SUSPENDED/COMPLETED 映射 |
| 8. 审批 | api/purchase approve→resume→execute_inbound_stock | **是**（resume/Command 走 graph） | **高** | 若改 workflow，必须保住 interrupt 位置 → resume 从同一点继续并到达可入库的终态；否则老 SUSPENDED 单无法补库 |
| 9. HITL | risk_agent interrupt(SUSPENDED)；前端待办 | **是** | **高** | 保留单一 interrupt 出入口；新增并行分支别多 interrupt |
| 10. Redis Lock | core/lock（审批并发锁） | 否 | 低 | 不动 |
| 11. Idempotency | core/idempotency（采购创建幂等） | 否（图外） | 低 | 不动 |
| 12. Redis Checkpointer | graph workflow 由 AsyncRedisSaver 编译；resume 依赖 | 是 | 中-高 | 不要换 checkpointer；审批恢复尽量不改变 thread_id 生成规则 |
| 13. API 接口 | 见上 | 部分 | 中 | 响应字段名不改（前端/测试依赖） |
| 14. 现有测试 | 一票集成/脚本调用（fastapi app 端对端、fastapi app 端对端我 CLI 跑的） | 高依赖 | 中-高 | 改 graph 后必须重跑「快进→触发→SUSPENDED→approve→补库」链路 & 价格/库存风险判定 |

凡标 **高**：因为“审批补偿库”“HITL”“Redis resume”都在 graph 语义内，**并行重构若动 interrupt/status 链路，极易打断老 SUSPENDED 单**。务必把 workflow 改造成“同一个初始 context 并行分析，但收敛后再同一把 interrupt/收尾”——这与当前“一节点 interrupt 挂在 risk_agent”不同，需保证收敛点一致。

---

## 14. Frozen / Reuse / Refactor Boundaries

### A. Frozen（禁止改，本次）
- `app/web/**`（前端 UI/布局/字段是客户认可项；重构不碰前端）
- 数据库 Schema / init.sql（不动表，不动字段）
- API 响应契约（字段名/状态值）——前端与测试依赖
- `core/lock`、`core/idempotency`、`core/redis`（并发/幂等/checkpoint 基建）
- 物理入库 `services/inventory.execute_inbound_stock`（“COMPLETED⟺入库”强绑定；不得改其原子性）
- `services/time_simulation` 的“扣库存”主逻辑（客户认可）

### B. Reuse（尽量复用，不重写）
- `app/services/**`（auto_procurement / auto_approval / time_simulation / inventory）
- `app/api/**`（create、approve、intent 等，graph 接口形态不变）
- `app/core/clock、core/db`
- `services.auto_procurement.scan_and_trigger_procurement`（巡检组装与触发逻辑，新建 Graph 也应保留相似入口）

### C. Refactor（本次才允许，且只动这一带）
- `app/graph/**`（state / workflow / nodes / analyzers / calculator）
- 新增：`app/graph/analyzers.py`（确定性分析工具）、`app/graph/calculator.py`、可选 `app/graph/decision.py`
- 决定把 demand 里 LLM only 文案移至“reasoning 后置”
- `workflow` 接线改成 (load-context) → fan(In 3 analyzers) → calculator → decision(policy) → (interrupt/end) （若做，用上面的收敛设计）

### D. Potentially Dangerous（易打坏现有功能）
- `nodes.risk_agent` 的 interrupt 位置 / 状态映射
- `workflow` resume 契约（审批路径）
- “quantity/total_amount 谁算谁消费”的顺序（若并行，consumer 必须等聚合；否则读到 None/0）
- Rabbit：`dashboard.py` 只消费 order，不对 graph 感知，改 graph 风险低，但改 state 名若被 `auto_procurement` 用则算高——注意 `auto_procurement` 组装 state 的 key 名字要与新 state 一致

---

## 15. Recommended Minimal Refactor Plan（最小，逐小步）

> 本次只审查，此节是“若你要重构，建议的顺序”。

1. **(不碰 DB/Front)** 在 `graph` 内把现有“决策数值”相关逻辑从“LLM/文案”分离，确认哪些输出仍必须落到 API（`quantity`/`status`/`risk`/`approved`/`__interrupt__`）。
2. 新增纯确定性 `analyzers`（库存健康 / 价格健康 / 金额+供应商声明）+ `calculator`（预测、缺口、数量、临时 should_purchase）。
3. 改造 `workflow`：保留“建单一 RUNNING”、“风险 interrupt SUSPENDED”，“approve→resume→最终入库”这些**对外契约**不动，仅把内部顺序改为 分析(可并行) → 计算 → 决策 → 收尾。
   保留只有一个 interrupt 出口（HITL 收敛点），避免多 interrupt 造成审批二义性。
4. 把 LLM 收敛为“文案后置”：demand_reasoning/风控自然语言在决策打定型后生成，绝不影响数字；失败自动 fallback 现文案。
5. 迁移 `quantity 决定者`：仍保证 quantity 精确来自计算器（=Calc），不给 LLM。
6. 逐步平行化实验（可先不并行，先把接线改为 fan-in；确认真并行由 LangGraph `StateGraph` 的 fan 即可，勿急着分布式调用 LLM）。
7. 集成本地回归：fastapi TestClient/脚本跑「快进→触发→SUSPENDED→approve→补库」「价格/库存/金额风险」「Idempotency/锁」全绿后，再上并行。

---

## 16. Risks and Open Questions

- R1：审批链路依赖 Graph resume 的**收敛点不可变**；并行化后若厂商一个“决策求和”，必须保证 HITL/interrupt 仍只发生一次、审批 resume 能回到同一 final 态 —— 否则老轮存单审批、兜底 `auto_approval` 会错。
- R2：`scan_and_trigger_procurement` 现在**内部先建单再 ainvoke**；若把 Graph 拆成“并行分析+决策”，其“入口一次”的建单（RUNNING）与中断(SUSPENDED) 的衔接必须仍旧。
- R3：Redis checkpointer 的 thread 结构若变多节点/并行，旧待审单（已存在于 Redis 的 session）在线程结构变化下 resume 语义需测试（需保留 thread_id 与 `Command(resume=..)` 契约）。
- R4：LLM（Ollama）延迟/不稳定 —— 并行好多个 LLM 更慢；只有文案走 LLM 才可控（可串后可并发做文案）。
- OQ1：是否真需要“Agent2 依赖 Agent1”？价格分析**不需要**，可独立并行；但“是否采购/买多少”需库存结论，所以在 Decision 层汇合，不必在 Price-Agent。
- OQ2：Agent3 的“供应商选择/可靠性”当前**无字段/无代码路径**（suppliers 每食材一行、且每次快照取首行；rating 字段存在但未用）。要不要为“比价/择商”新增 DB 查询或多供应商建模 → 这属于“补充 agent能力”，本次不建议做（超出重排编排层；改了会动 schema）。

---

## 17. Final Architecture Decision

**裁决（供你人工定夺）：**

- ✅ **接受方向**：把“采购链路”重组为「**并行事实分析 → 确定性 Calculator → 规则决策 → (风险)HITL / Auto → Purchase**」这个**骨杠**是合理的；它能带来阅读性、可测性、以及对“该不该买/为何买→该让人看什么”的清晰边界。
- ⚠️ **不要**做“每个 Agent 都 LLM it 化”：库存/价格/数量/金额等全为确定性领域逻辑，LLM 只应做解释/风控自然语言。并行化应并行**分析工具**，而非并行 3 个 LLM。
- ⚠️ 必须保住对外契约：图外建单/落库、`risk_agent` 单一 interrupt(SUSPENDED)、approve resume→`execute_inbound_stock`、`auto_approval` 兜底、状态值模型（RUNNING…COMPLETED）与字段名——否则审批补库与历史单会回归。
- ✅ **若你这么重构**，改动范围严格限定在 **`app/graph/**`（+ 可新增 analyzers/calculator）**，Frozen=web/schema/core-infra/services-inventory；DB/前端/API 契约一律不动。
- 不建议本次增补“供应商比价/择商”“新多个 Agent”（会出现 Agent 名目需求，但数据/路径不足，还需改 schema）。

---

Audit Mode: READ ONLY
Code Changes: NONE
Business Logic Changes: NONE
Database Changes: NONE
Frontend Changes: NONE

Pre-existing changes detected（审查前工作区即存在，未改动/未清理）:
- 未跟踪文件：`architecture.png`（本人先前架构图产物，非业务代码，未纳入 git；未清除）
  - `git status` 仅此一项；无其它被修改的跟踪文件。
