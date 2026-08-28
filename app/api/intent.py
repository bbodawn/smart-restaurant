from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.llm import get_structured_llm
from app.schemas.intent import IntentParseResult

router = APIRouter(tags=["intent"])

class IntentParseRequest(BaseModel):
    text: str = Field(..., description="用户的自然语言采购需求文本", min_length=2)

class MatchedItem(BaseModel):
    raw_ingredient: str = Field(description="用户输入的原始食材名称")
    matched_ingredient: Optional[str] = Field(description="数据库中匹配到的标准食材名称，未匹配到则为 null")
    specified_quantity: Optional[float] = Field(description="用户指定的采购数量（kg）")
    is_valid: bool = Field(description="数据库中是否存在该食材")

class IntentParseResponse(BaseModel):
    user_intent_summary: str
    parsed_items: List[MatchedItem]

@router.post("/parse-intent", response_model=IntentParseResponse)
async def parse_user_intent(
    request: IntentParseRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        # 1. 调用 LLM 进行结构化抽取
        structured_llm = get_structured_llm(IntentParseResult, temperature=0.0)
        llm_result: IntentParseResult = await structured_llm.ainvoke(request.text)

        # 2. 查询数据库中的已知食材，进行匹配校验
        db_res = await db.execute(text("SELECT name FROM ingredients"))
        existing_ingredients = {row[0] for row in db_res.fetchall()}

        matched_items: List[MatchedItem] = []
        for item in llm_result.items:
            is_valid = item.ingredient in existing_ingredients
            matched_items.append(
                MatchedItem(
                    raw_ingredient=item.ingredient,
                    matched_ingredient=item.ingredient if is_valid else None,
                    specified_quantity=item.specified_quantity,
                    is_valid=is_valid,
                )
            )

        return IntentParseResponse(
            user_intent_summary=llm_result.user_intent_summary,
            parsed_items=matched_items,
        )

    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to parse intent: {str(exc)}")
