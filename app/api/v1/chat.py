from __future__ import annotations

from fastapi import APIRouter, Depends, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.chat import (
    AddToInventoryRequest,
    ChatRequest,
    ChatResponse,
    ConfirmTransactionRequest,
    CustomerConfirmRequest,
    SkipProductRequest,
)
from app.services.chat_service import (
    add_to_inventory_and_resume,
    confirm_customer,
    confirm_transaction,
    handle_message,
    skip_product_and_resume,
)
from app.services.transcription_service import transcribe_audio

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


@router.post("/transcribe/", response_model=dict)
async def transcribe(
    audio: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
) -> dict:
    text = await transcribe_audio(audio)
    return {"text": text}


@router.post("/confirm-customer/", response_model=ChatResponse)
async def confirm_customer_endpoint(
    payload: CustomerConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    return await confirm_customer(db, current_user.id, payload)


@router.post("/confirm-transaction/", response_model=ChatResponse)
async def confirm_transaction_endpoint(
    payload: ConfirmTransactionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    return await confirm_transaction(db, current_user.id, payload)


@router.post("/add-to-inventory/", response_model=ChatResponse)
async def add_to_inventory_endpoint(
    payload: AddToInventoryRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    """Bug 3: User taps 'Add to Inventory' for a not-found product.
    Saves the product then resumes the pending order pipeline."""
    return await add_to_inventory_and_resume(db, current_user.id, payload)


@router.post("/skip-product/", response_model=ChatResponse)
async def skip_product_endpoint(
    payload: SkipProductRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    """Bug 3: User taps 'Skip & Continue' for not-found product(s).
    Removes them from the order silently and resumes the pipeline."""
    return await skip_product_and_resume(db, current_user.id, payload)
