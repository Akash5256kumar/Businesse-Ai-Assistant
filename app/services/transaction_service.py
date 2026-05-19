from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.transaction import Transaction
from app.services.inventory_service import adjust_stock

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

    for item in (items or []):
        if isinstance(item, dict) and item.get("name"):
            qty = Decimal(str(item.get("quantity", 0)))
            rate = Decimal(str(item["rate_per_unit"])) if item.get("rate_per_unit") else None
            await adjust_stock(db, user_id, item["name"], -qty, item.get("unit", "piece"), sale_price=rate)

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

    for item in (items or []):
        if isinstance(item, dict) and item.get("name"):
            qty = Decimal(str(item.get("quantity", 0)))
            rate = Decimal(str(item["rate_per_unit"])) if item.get("rate_per_unit") else None
            await adjust_stock(db, user_id, item["name"], qty, item.get("unit", "piece"), purchase_price=rate)

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
