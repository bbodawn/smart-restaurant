# 我的项目到底有什么能力 · Current Capabilities

> 严格区分三档：**已经实现** / **有边界(部分实现在字段等)** / **明确未实现**。
> 不把它做得比真实还高级——防止你以后在面试或谈项目时讲错、讲虚。

---

## 一、已经实现（MVP 真实可用，均经真实 MySQL+Redis 测试）

### 采购流程与决策链
- **并行三分析（确定性）**：库存 / 价格 / 供应商三个分析节点在同一工作流里并行（Fan-out）再汇聚到制度表（Fan-in）。
- **3 天预测口径**：采购/分析沿用 `需求 ≈ 日销量 × 3` 的简单确定性预测。
- **Deterministic Policy**：按写死规则给 `NO_PURCHASE / PURCHASE / REVIEW`，并产出 `quantity / total_amount / risk_flags`，是采购数量的唯一来源。
- **价格偏差**：`|现值−历史均价|/历史均价`，历史均价<=0 时安全回退 0（不除零）。
- **Agent5（LLM）风险解释**：仅在 REVIEW 出现，生成给人工看的报告(supr风险概况/等级/分析/建议)。它**不**决策采购、**不**碰数据库。
- **HITL 人工审批**：REVIEW → 停在 `interrupt`，等人类点通过/拒绝再恢复（真实实现，Redis checkpoint 支持）。
- **REVIEW→SUSPENDED 而非自动落库**：高风险单不会在无人工时自动入库。

### 恢复与持久化
- **Redis Checkpoint**：保存工作流中断后的执行点与状态 → approve/reject 后从该点续跑，不重跑 START。
- **MySQL 持久化**：真实订单, `purchase_order_items.quantity`, `inventory.current_stock`, 供应商等长期存库。

### 自动采购与执行
- **Auto Procurement**：定时巡检低于「3 天需求」的食材，并让 `deterministic_policy` 决定自动(PURCHASE)或请人(REVIEW)。候选范围已与制度表对齐（踩坑后修正）。
- **Service + 事务入库**：`execute_inbound_stock()` 在一个 DB 事务里“+库存 & 置订单完成”，保证原子。
- **入库幂等**：订单已完成的那张单不会再次 +库存。

### 并发保护
- **Redis Ingredient Lock**：自动采购按“食材”上非阻塞锁，防多 Worker 重复下单。
- **DB open-order 检查（`_has_open_order`）**：锁失效时的账本兜底。
- **Approval 侧再保护**：审批接口有 order 粒度 Redis 锁 + DB 状态检查（防双击 / 通过与拒绝并发乱序）。
- 真实并发验证过：同食材两 Worker 最终只有 1 单 / 库存只 +N(一次)。

---

## 二、部分实现 / 有边界（使用时要如实说明）

- **“Analysis/Agent”命名**：Agent1/2/3 其实是确定性 Python 分析角色，不是 LLM Agent；只有 Agent5 是 LLM。称它们“Agent”便于表达分步，但别对外说“Agent1/2/3 也是大模型智能体”。
- **auto 与审批自动放行**：有“超阈值自动放行”的后台服务（真单会走审批逻辑），看业务是否需要；它不是“自动审批所有”。
- **健康库存判断**：只用“3 天需求(一个固定阈值)”这套确定性判断；它有相对简单，不要表述成靠谱的“3～7 天健康区间”。
- **Supplier Analysis**：只输出当前 supplier 快照；**不是**多供应商比价选优。

---

## 三、明确未实现（诚实清单，别编）

- 真正的 3~7 天健康库存区间
- 高级需求预测模型（LSTM/回归等）
- 真正的多供应商比较/自动选商
- Supplier 综合评分体系
- 采购价格趋势预测 / 成本优化
- 在途库存管理与 eta 预占
- ABC（按重要性分管理）+ 批次/临期（保质期）管理
- 更复杂的补货策略 / 自动调安全线
- RBAC/账号权限/多用户审批流
- 完整生产级监控 / 追�计 / 告警 / 审计日志系统
- Redis HA、锁续租(Watchdog)、Redlock 等生产级分布式锁机制
- 消息队列/dead-letter/任务队列化

这些都是“以后若要更生产化”的方向，**现在代码里没有**，别假想已落地。

---

## 一句话边界图

确定性算术+制度=给 Python；风险解释=给 Agent5(LLM)；最终授权=给 HITL 的人类；真正下单+改库存+事务+幂等=给 Service+MySQL；并发互斥= Redis Ingredient Lock(+DB检查)；恢复续跑=Redis Checkpoint。这套 MVP 的每一环都在真实环境验证过；上面的“未实现清单”暂不追求，别把“未做”当成“已有”。
