from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer

_HONORIFICS = re.compile(
    r"\s*(ji|bhai|bhaiya|sahab|sahib|sir|madam|ben|didi)\s*$",
    re.IGNORECASE,
)


def _clean_name(name: str) -> str:
    return _HONORIFICS.sub("", name.strip()).strip()


async def get_or_create(db: AsyncSession, user_id: int, name: str) -> Customer:
    normalised = name.strip()
    result = await db.execute(
        select(Customer).where(
            Customer.user_id == user_id,
            Customer.name.ilike(normalised),
        )
    )
    customer = result.scalar_one_or_none()

    if customer is None:
        customer = Customer(user_id=user_id, name=normalised)
        db.add(customer)
        await db.flush()

    return customer


async def search_by_name(db: AsyncSession, user_id: int, name: str) -> list[Customer]:
    """Partial name search after stripping honorifics. Returns up to 5 results."""
    clean = _clean_name(name)
    result = await db.execute(
        select(Customer)
        .where(Customer.user_id == user_id, Customer.name.ilike(f"%{clean}%"))
        .order_by(Customer.name)
        .limit(5)
    )
    return list(result.scalars().all())


async def get_by_id(db: AsyncSession, customer_id: int) -> Customer | None:
    return await db.get(Customer, customer_id)


async def get_by_phone(db: AsyncSession, user_id: int, phone: str) -> Customer | None:
    result = await db.execute(
        select(Customer).where(Customer.user_id == user_id, Customer.phone == phone.strip())
    )
    return result.scalar_one_or_none()


async def create_customer(
    db: AsyncSession, user_id: int, name: str, phone: str | None = None
) -> Customer:
    customer = Customer(user_id=user_id, name=name.strip(), phone=phone)
    db.add(customer)
    await db.flush()
    return customer
