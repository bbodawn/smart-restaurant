from typing import Optional, TypedDict

class PurchaseState(TypedDict, total=False):
    order_id: int
    order_no: str
    thread_id: str

    ingredient_id: int
    ingredient: str
    unit: str

    current_stock: float
    daily_sales: float
    predicted_demand: float
    quantity: float

    supplier_id: int
    supplier_name: str
    supplier_price: float
    historical_price: float

    price_deviation: float
    total_amount: float

    risk_reason: Optional[str]
    approved: Optional[bool]

    status: str
