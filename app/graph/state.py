from typing import Optional, TypedDict

class PurchaseState(TypedDict, total=False):
    order_id: int
    order_no: str
    thread_id: str

    ingredient_id: int
    ingredient: str
    unit: str

    current_stock: float
    safety_stock: float
    daily_sales: float
    predicted_demand: float
    quantity: float
    demand_reasoning: str

    supplier_id: int
    supplier_name: str
    supplier_price: float
    historical_price: float

    price_deviation: float
    total_amount: float
    risk_analysis_report: Optional[str]

    extension_agent_analysis: Optional[str]

    # 并行分析结果容器（本轮 Phase 3-A 引入，作为只读分析层输出，不落数据库）
    analysis_inventory: Optional[dict]
    analysis_price: Optional[dict]
    analysis_supplier: Optional[dict]

    # 确定性业务规则层输出（Phase 3-A 引入；status/quantity/total_amount/risk_flags/risk_reason）
    policy_decision: Optional[dict]

    # Agent 5 风险综合分析输出（Phase 3-B Step 2；summary/risk_level/risk_analysis/recommendation）
    agent5_analysis: Optional[dict]

    risk_reason: Optional[str]
    approved: Optional[bool]

    status: str
