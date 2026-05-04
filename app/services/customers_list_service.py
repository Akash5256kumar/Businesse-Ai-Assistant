from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.transaction import Transaction
from app.schemas.customers import (
    CustomerListItem,
    CustomerListResponse,
    CustomerTransactionItem,
    CustomerTransactionListResponse,
)


async def get_customers_page(
    db: AsyncSession,
    user_id: int,
    *,
    page: int,
    page_size: int,
) -> CustomerListResponse:
    offset = (page - 1) * page_size

    count_result = await db.execute(
        select(func.count()).select_from(Customer).where(Customer.user_id == user_id)
    )
    total_count: int = count_result.scalar_one()

    result = await db.execute(
        select(Customer)
        .where(Customer.user_id == user_id)
        .order_by(Customer.pending.desc(), Customer.name)
        .offset(offset)
        .limit(page_size + 1)
    )
    rows = list(result.scalars().all())
    has_more = len(rows) > page_size
    customers = rows[:page_size]

    return CustomerListResponse(
        items=[
            CustomerListItem(
                id=c.id,
                name=c.name,
                phone=c.phone,
                balance=float(c.pending),
                updated_at=c.updated_at,
                total_sale=float(c.total_sale),
                total_received=float(c.total_received),
            )
            for c in customers
        ],
        page=page,
        has_more=has_more,
        total_count=total_count,
    )


async def get_customer_transactions(
    db: AsyncSession,
    user_id: int,
    customer_id: int,
    *,
    page: int,
    page_size: int,
) -> CustomerTransactionListResponse:
    offset = (page - 1) * page_size

    result = await db.execute(
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.customer_id == customer_id,
        )
        .order_by(Transaction.created_at.desc())
        .offset(offset)
        .limit(page_size + 1)
    )
    rows = list(result.scalars().all())
    has_more = len(rows) > page_size
    transactions = rows[:page_size]

    return CustomerTransactionListResponse(
        items=[
            CustomerTransactionItem(
                id=tx.id,
                type=tx.type,
                amount=float(tx.amount),
                is_credit=tx.is_credit,
                note=tx.note,
                created_at=tx.created_at,
            )
            for tx in transactions
        ],
        page=page,
        has_more=has_more,
    )
