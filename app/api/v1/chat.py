from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.chat import ChatRequest, ChatResponse, CustomerConfirmRequest
from app.services.chat_service import confirm_customer, handle_message

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


@router.post("/", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    return await handle_message(
        db,
        current_user.id,
        payload.message,
        raw_text=payload.raw_text,
        script=payload.script,
        lang_hint=payload.lang_hint,
    )


@router.post("/confirm-customer/", response_model=ChatResponse)
async def confirm_customer_endpoint(
    payload: CustomerConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    return await confirm_customer(db, current_user.id, payload)
