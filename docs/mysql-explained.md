# MySQL 在这里到底负责什么 · MySQL Explained

> 一句话主题：**LangGraph State 是“这次流程用到的临时数据”，MySQL 才是“真正长期保存的业务数据与库存”。**
> 别把“决定买 20”当已经入库 20——决定在 State，真正数字落账到 MySQL。

---

## 1. 记忆一张图：谁“临时”、谁“长期”

| 位置 | 代表 | 生命周期 |
| --- | --- | --- |
| LangGraph State（内存/流程内） | `policy_decision.quantity`＝“这次我们认为该买 20” | 一次流程跑完就算（可被 checkpoint 存档/丢弃） |
| MySQL `purchase_orders` | 这一张采购单及它的生命状态 | 长期保存 |
| MySQL `purchase_order_items` | “这一单里某食材要买多少” `quantity` | 长期保存 |
| MySQL `inventory`（食材的 current_stock） | “这家仓库现在真正剩多少” | 长期保存，唯一真账 |

一句话：
- State 那个数只是讨论结论；
- 只有 Service 真正把 `policy_decision.quantity` 写进 `purchase_order_items.quantity`，并且执行把 `inventory.current_stock` +N 的入库动作，库存数据才算变了。

> **没有 MySQL 的确认，就没有“真买到了”。**

---

## 2. 为什么要 MySQL，而不用“一直放在程序内存里”

- 服务可能重启、有多实例，内存说丢就丢；
- 需要一份**一打开就能看到历史、改完了还在**的持久化数据（订单、库存）；
- 需要数据库层的事务和约束来兜业务正确性。

于是我们把 MySQL 当“记账本”，Redis 当“临时内存档/门锁”（见 `redis-explained`）。两边定位不同：MySQL 是 source of truth(真账)，Redis 是辅助层。

---

## 3. 三个数字为什么要分开（重要的区分）

假设计算：库存现 40，3 天需求 60 ⇒ 需要采购 20。

1. `policy_decision.quantity = 20`
   —— 只是“这套规则认为该买 20”的**决策快照**，放在本次流程 State。
2. `purchase_order_items.quantity = 20`
   —— Service 真正把采购数量**写进订单明细**（落 MySQL），表示这张单要买 20。
3. `inventory.current_stock += 20`
   —— 最终 `execute_inbound_stock()` 执行把数据库里的库存+20，仓库才真的多了 20。

三步并不总是同时发生：尤其 REPLIT…… 停下来重点记忆——“决定买 20”一定先于“订单里写 20”，而且要真做了入库动作，库存数字才变。这也是旧代码出 bug 的坑（详见 problem-history：数量曾放错位，导致看着买了却没入库）。

---

## 4. 落到数据库里的主要表（非常简化的认识）

- `purchase_orders`：一进系统建一张单，存状态一路 RUNNING→SUSPENDED(待人工) / COMPLETED / REJECTED，金额等。
- `purchase_order_items`：这一张单具体买哪个食材、数量、单价、金额。
- `ingredients`：食材基础信息（面粉、猪肉…）。
- `inventory`：每个食材的当前库存、日销量、安全线。
- `suppliers`：供应商与报价。

系统核心把“要买多少”落到 `items.quantity`，并通过 `execute_inbound_stock` 同步累加 `inventory.current_stock`、把订单标完成。

---

## 5. 事务为什么对这件事很重要（两分钟直觉）

当 Service 执行入库时，它希望这两件事“要么都成、要么都不成”：

- 库存 +N
- 订单标成 COMPLETED

如果只 +了库存却没标完成，重启后可能重复入库或流程混乱。因此把“加库存 + 改订单状态”放在同一个数据库事务（Transaction）里一次提交——保证原子。任何一步出错整段回滚，不会出现“库存多了但订单没完成”这种半吊子状态。

> 概念理解：事务像一个“捆很紧的一步”：成则全都成，败则全都不留下。

更严密的数据库一致性与重复保护在 `concurrency-and-idempotency` 里讲；这篇重在讲“MySQL State vs MySQL 分工”这个地基。

---

## 6. 面试一句话

问“LangGraph State 怎么不存业务”：
答：State 保存的是**这一次流程内部的临时结论**（例如 `policy_decision`）；业务真账与库存放在 MySQL——Service 把决策的量化成订单明细并事务地入库，MySQL 是长期真账，内存 State 可随时丢弃或由 checkpoint 重建。二者职责边界清晰，绝不混。
