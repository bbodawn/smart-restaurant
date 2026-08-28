from typing import List, Optional
from pydantic import BaseModel, Field

class PurchaseItemIntent(BaseModel):
    ingredient: str = Field(description="食材名称，如：猪肉、鸡肉、大米")
    specified_quantity: Optional[float] = Field(
        default=None,
        description="用户明确指定的采购数量（单位：kg），若用户未说明具体数量则填 null"
    )

class IntentParseResult(BaseModel):
    items: List[PurchaseItemIntent] = Field(description="解析出的食材采购列表")
    user_intent_summary: str = Field(description="用户输入的原始意图一句话总结")
