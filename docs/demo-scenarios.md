# Demo 场景（Phase 7-A）

三个演示场景均由真实 MySQL + Redis + LangGraph 的 LIVE 用例承载（`TEST_LIVE=1` 运行），
非 mock。Dashboard 为真实 `/api/v1/dashboard`。

| 场景 | 输入 | 期望 | 承载验证（LIVE 用例） |
|---|---|---|---|
| **S1 自动采购** | 库存低于 3 日需求、价格正常、供应商正常 | `source=AUTO`, `status=COMPLETED`；Dashboard 显示 🤖 自动采购 | `tests/live/test_phase6d_dashboard_fields.py::test_dashboard_auto_order_fields` |
| **S2 风险采购（人工审批）** | 库存不足 + 价格异常上涨 | Agent5 生成 canonical 分析；`SUSPENDED`；Dashboard 显示 👤 人工审批 + 风险等级 + AI 分析 | `tests/live/test_phase6c_agent5_snapshot.py::test_manual_review_snapshot_persisted` 与 `test_dashboard_review_manual_fields` |
| **S3 人工拒绝** | REVIEW 订单 → reject | `status=REJECTED`；库存不增加；订单保留审批审计（approval_reason / rejected_virtual_date） | `tests/live/test_phase6b2_manual_flow.py::test_manual_review_reject` |

演示操作建议：
1. 启动脚本 `scripts/start.sh`（Redis/MySQL/Ollama）。
2. `TEST_LIVE=1 ./.venv/Scripts/python.exe -m pytest -q`（全绿基线）。
3. 跑上述场景用例确认；或在 Dashboard 用"新增食材/快进天数"触发自动采购，
   再对价格异常食材手动建单触发 REVIEW → approve / reject 观察订单卡变化。
