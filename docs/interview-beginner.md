# 面试时我怎么讲这个项目 · Interview for Beginners

> 不是背八股，而是用“我真实做过的事”把技术讲出来。
> 每个问题按：问 → 小白理解 → 正式回答 → 本项目真实例子 → 面试官追问 → 我的回答。

---

## 0. 如果只有 1 分钟介绍

“我做的是一个智能食堂采购系统：每天自动检查哪些食材未来 3 天可能不够，然后并行分析库存、价格与供应商；一个写死的规则裁判决定是自动补货、还是高风险需要人工拍板。普通低风险自动入库；高风险会让一个 LLM 顾问把风险讲成报告，停下来交给人类审批，通过后才真正增加库存。Redis 负责两件事——给流程存续跑的点、给并发当锁防重复采购；MySQL 才是保存订单和库存的真账。这套多 Agent + 人工审批 + 并发保护链在真实 MySQL/Redis 上都验证过。”

---

## 1. 项目整体 / 为什么多 Agent

**问：这个项目是做什么的？为什么用多 Agent？**
- 小白理解：就像食堂来了个自动算账员，看到谁要断货就按制度判断补不补，风险高的请领导批。
- 正式：食材采购被拆成多角色流水（分析→制度→人审→执行），每步单一职责、独立测试。
- 真实例子：库存/价格/供应商三根分析只读、互不依赖，因此并行(Fan-out)再汇到 `determinetic_policy`(Fan-in)。
- 追问：都是 Agent 为什么不用在大模型？→ 因为这些 Agent 是确定性角色，用固定代码更可控；真正非确定性、需要开口解释的只有 Agent5。
- 我的回答：多 Agent 不必然等于多 LLM；让重复可测的“算账角色”走代码，用 LLM 的那一小块只放解释。

**问：为什么不是普通 Python 函数从头串到尾？**
- 小白理解：流程中间要能停下来等人、等下还能接着跑，不是一份单程脚本。
- 正式：需要并发、条件分支、interrupt/恢复（Checkpoint），LangGraph 把这些表达成显式图。
- 真实例子：REVIEW 中间 interrupt 等人；approve 后从存档继续而不是重跑三个分析。
- 追问：跟任务队列比呢？→ 队列适合放事件，这里是人参与的状态机流程，LangGraph 更贴合可恢复的图编排。

---

## 2. Agent / Workflow

**问：为什么 Agent1/2/3 不用 LLM？**
- 小白理解：它们在查表做算术——让一个会聊天的去口算买多少，既慢还可能算错。
- 正式：确定性计算须可预测、可审计；库存/价格/供应商分析是只读算术（含价格偏差、需求乘法）。
- 真实例子：`daily_sales * 3`、绝对值偏差都在 Python 内完成。
- 追问：可以都换 LLM 吗？→ 会引入不确定性、更贵、难测试，还要处理幻觉数量——不划算。

**问：什么 Deterministic Policy？**
- 小白理解：一张写死的采购制度表。谁该补、买多少、要不要人审全按 if/规则。
- 正式：把三个分析 facts 映射到 `NO_PURCHASE / PURCHASE / REVIEW`，产出 `quantity / total_amount / risk_flags`。
- 真实例子：`stock>=3天 需求且 >safety → NO_PURCHASE` 这类。
- 追问：为什么不把判断交给 LLM？→ 制度要白纸黑字可审计，数量/金额不能靠模型撞运气。

**问：Agent5 到底干什么？为什么不能直接决定？**
- 小白理解：它是风险顾问，讲清楚“为什么危险、建议怎么处理”，无权自己批或改单。
- 正式：只读前面 facts，返回结构化建议(概况/风险等级/解释/建议)，不动 quantity/status、不写 DB、不绕过 HITL；最终授权归人工。
- 真实例子：REVIEW 后先出 `agent5_analysis`，流程再去等人点通过/拒绝。
- 追问：LLM 挂了怎么办？→ 安全回退：保守兜底文案，不让模型幻觉成为采购决定，仍保持 REVIEW 等人工。

**问：为什么三个分析可以并行？**
- 小白理解：三个人各查各的、谁也不押住谁，看完一起汇报。
- 正式：它们只读、写不同的 channel（`analysis_inventory/price/supplier`），无相互依赖，并行安全。
- 追问：会写坏同一字段吗？→ 不会，各自写独立字段，最后再 Fan-in。

---

## 3. HITL

**问：什么是 HITL？为什么 REVIEW 必须人工？**
- 小白理解：出格/危险的情况请真人拍板，符合预算和责任。
- 正式：高风险采购不自动落库，REVIEW 会进入 Human-in-the-loop 的人工审批。
- 真实例子：价格>20% 或库存贴线单会 SUSPENDED，等人点 approve/reject。
- 追问：凭什么系统不能自己定？→ 责权重大、需要考虑行情/责任，机器不背这个决策质量锅。

**问：interrupt() 与 APPROVE 后为什么能续跑不重来？**
- 小白理解：做到一半存档，被批后从存档接着做。
- 正式：`interrupt` 把运行停在某 node 并存 Checkpoint(在 Redis)，`Command(resume=...)` 从该 node 继续。
- 真实例子：REVIEW resume 后从 `purchase_approval`/ `purchase_agent` 继续，不重跑三角色。
- 追问：服务重启丢失怎么办？→ 有从 DB 重建字段兜底，Redis 不可用前/后都有预案。

---

## 4. Redis / 并发

**问：Redis Checkpoint 和 Lock 有什么区别？**
- 小白理解：一个管“记住做到哪一步”，一个管“别两个人碰同一个”。
- 正式：Checkpoint=工作流时间旅行状态；Lock=进程间互斥。
- 真实例子：Checkpoint 帮人工审批恢复；Lock 防 auto 双Worker 同食材。
- 追问：为什么都在 Redis？→ 需要极快的共享；是同一介质两职责，不是同一功能。
- 我的回答：一定分开讲，别把它俩混成一个“Redis 干了一件”.

**问：为什么要 Ingredient Lock 又还要 DB 检查与入库幂等？**
- 小白理解：把门、再对一遍账、最后发货系统自己不重发，三层不是一回事。
- 正式：
  1. Ingredient Redis Lock：把查单→下单→入库串行；
  2. `_has_open_order()`：锁失效时以账本再兜底；
  3. `execute_inbound_stock` 幂等：已 COMPLETED 不二次 +库存。
- 真实例子 / 试验结论：同食材两 Worker 并发，最终只产生 1 单、库存只 +N 一次。

**问：什么是 check-then-act？为什么它不够？**
- 小白理解：先看再动手——但“看”那一下两人都看到空，后手还是会同时做。
- 正式：读后-写模式在缺少互斥时不原子。
- 真实例子：A/B 都查到「无进行中单」然后各自建单，重复。
- 追问：只用 database unique 行不行? → 不同 orderId 可买同 ingredient；唯一键(同order+同ingredient)拦不住“两张不同单各买一次”。
- 我的回答：并发下「单独检查」不保险，须加锁来包裹临界，再靠幂等兜底。

**问：execute_inbound_stock 为什么还要幂等？**
- 小白理解：已发货的箱，不能因为扫第二次又发一箱。
- 正式：对已完成订单再次执行不会 +库存，返回“已完”。
- 我的回答：(给具体例子：双击 approve / 进程重放不会把库存加两次)

---

## 5. 数据与业务

**问：policy_decision.quantity 为什么是唯一真相？**
- 小白理解：所有模块都读同一个小便签上的“该买多少”，别各读各的。
- 正式：数据契约 — Service 以 `policy_decision.quantity` 写入 `purchase_order_items.quantity` 再入库。
- 真实踩坑：曾经 service 读旧顶层 `state["quantity"]`(一直是0)，出现“看着购买成功实际没入库”。
- 追问：为什么有这个历史坑 → 新流程把数量挪进 policy，但读取方还照旧拿旧字段。
- 我的回答：统一 source-of-truth，并附契约注释，面试时能讲这个真实案例很有分量。

**问：Graph State 和 MySQL 分别拿什么？**
- 小白理解：一个是用一次的共享草稿，一个是记总账。
- 正式：State 是一次流程的临时态，可丢弃/重建；MySQL 是全球长期持久化 + 事务。
- 追问：能把 State 当数据库吗？→ 不能：内存会丢、在多实例不共享、也无事务。

**问：为什么三种 status？(`policy_decision.status/state.status/orders.status`)**
- 小白理解：一张纸条(决策)、过程(流程)、账本(业务/审批)是三种不同层次的“是不是该买/进行到哪”。
- 正式：分决策-运行-业务三层，便于责任制，也避免一个字段塞不下多种含义。
- 追问：能合并吗？→ 会分不清“这次决策结论”与“真账上的状态”，审计混乱。

**问：自动采购和人工采购的差别？**
- 小白理解：一条是自动走完自动入库，一条中间停下来请领导批。
- 正式：PURCHASE→purchase_agent 直接走执行；REVIEW→HITL 审批后才继续、整体复用 Service 入库函数。
- 我的回答：触发不同、职责共享底(Service execute_inbound_stock)。

---

## 6. 四个真实坑怎么跟面试官讲

### 坑A：扫描范围和 Policy 不一致
- 讲：我最初自动巡检只找“≤安全库存”，结果自动扫中的全进 REVIEW，能自动补的 PURCHASE 几乎不发生；后来把入口调成与 policy 同一条 3 天预测线。→ 学：入口条件必须和核心规则对齐。

### 坑B：quantity 契约冲突
- 讲：policy 判要买 20 但 service 读旧字段 state.quantity=0，看起来建单了却从不入库存；统一以 policy_decision.quantity 为 source-of-truth。→ 学：多模块对“数据到底放哪”要有一致约定。

### 坑C：REVIEW 没有真正进 HITL
- 讲：若把 REVIEW 当成“没中断=没风险自动执行”，高风险会绕过人工；改成 REVIEW→Agent5→HITL 强制人工。→ 学：风险分支必须显式进人工审批。

### 坑D：多 Worker check-then-act 竞态
- 讲：两人同时“查无单”各自建 → 重复；加 Ingredient Lock + DB open 检查 + 入库幂等三层，真实并发只出一单一次入库。→ 学：并发下要加锁包裹关键区，再加幂等兜。

---

### 我现在应该真正记住的 10 件事

1. 采购数量唯一真相在 `policy_decision.quantity`，不是旧的 `state.quantity`。
2. REVIEW 一定要经 Agent5 + HITL，禁止把它当无风险自动执行。
3. 自动采购的入口要和新 Policy 同理（<3 天需求），别只扫安全线。
4. 并发下“查一下再做”不保险，关键区要给 Redis Ingredient Lock。
5. Redlock / watchdog / Redis HA / 真 3~7 天区间、多供应商智能选优 = **都没实现**，别吹。
6. Agent1/2/3 是确定性 Python，不是 LLM；只有 Agent5 用 LLM 做题。
7. Redis Checkpoint ≠ Redis Lock——一个是存档续跑，一个是并发锁。
8. Graph State ≠ MySQL——一临时草稿，一长期账本（事务）。
9. 库存的真正变化在 `execute_inbound_stock()`，且它做完已 COMPLETED 就幂等不再 +。
10. 项目最高价值点是“责任分离”：确定性→代码，解释→Agent5，授权→HITL人，真改账→Service/MySQL。
