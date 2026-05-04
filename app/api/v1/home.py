from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.home import (
    HomeResponse,
    TransactionDetailResponse,
    TransactionListResponse,
)
from app.services.home_service import (
    get_home_data,
    get_transaction_detail,
    get_transactions_page,
)

router = APIRouter(prefix="/api/v1/home", tags=["home"])


@router.get("/", response_model=HomeResponse)
async def home(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HomeResponse:
    return await get_home_data(db, current_user.id)


@router.get("/transactions", response_model=TransactionListResponse)
async def transactions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TransactionListResponse:
    return await get_transactions_page(
        db,
        current_user.id,
        page=page,
        page_size=page_size,
    )


@router.get("/transactions/{transaction_id}", response_model=TransactionDetailResponse)
async def transaction_detail(
    transaction_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TransactionDetailResponse:
    return await get_transaction_detail(db, current_user.id, transaction_id)
