from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.customers import CustomerListResponse, CustomerTransactionListResponse
from app.services.customers_list_service import (
    get_customer_transactions,
    get_customers_page,
)

router = APIRouter(prefix="/api/v1/customers", tags=["customers"])


@router.get("/", response_model=CustomerListResponse)
async def list_customers(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CustomerListResponse:
    return await get_customers_page(
        db, current_user.id, page=page, page_size=page_size
    )


@router.get("/{customer_id}/transactions", response_model=CustomerTransactionListResponse)
async def customer_transactions(
    customer_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CustomerTransactionListResponse:
    return await get_customer_transactions(
        db, current_user.id, customer_id, page=page, page_size=page_size
    )
