from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.transaction import Transaction

ZERO = Decimal("0")


async def record_sale(
    db: AsyncSession,
    user_id: int,
    customer: Customer,
    amount: Decimal,
    pending_amount: Decimal,
    is_credit: bool = False,
    items: list | None = None,
    note: str | None = None,
) -> Transaction:
    tx = Transaction(
        user_id=user_id,
        customer_id=customer.id,
        type="sale",
        amount=amount,
        pending_amount=pending_amount if is_credit else None,
        is_credit=is_credit,
        items=items or [],
        note=note,
    )
    db.add(tx)
    customer.pending += pending_amount
    customer.total_sale += amount
    return tx


async def record_payment(
    db: AsyncSession,
    user_id: int,
    customer: Customer,
    amount: Decimal,
    note: str | None = None,
) -> Transaction:
    tx = Transaction(
        user_id=user_id,
        customer_id=customer.id,
        type="payment",
        amount=amount,
        is_credit=False,
        note=note,
    )
    db.add(tx)
    customer.pending = max(customer.pending - amount, ZERO)
    customer.total_received += amount
    return tx


async def record_purchase(
    db: AsyncSession,
    user_id: int,
    amount: Decimal,
    items: list | None = None,
    note: str | None = None,
) -> Transaction:
    tx = Transaction(
        user_id=user_id,
        customer_id=None,
        type="purchase",
        amount=amount,
        is_credit=False,
        items=items or [],
        note=note,
    )
    db.add(tx)
    return tx


async def record_expense(
    db: AsyncSession,
    user_id: int,
    amount: Decimal,
    note: str | None = None,
) -> Transaction:
    tx = Transaction(
        user_id=user_id,
        customer_id=None,
        type="expense",
        amount=amount,
        is_credit=False,
        note=note,
    )
    db.add(tx)
    return tx
