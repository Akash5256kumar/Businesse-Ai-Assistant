from __future__ import annotations

import re
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.inventory import Inventory
from app.models.transaction import Transaction
from app.schemas.inventory import InventoryItemResponse, InventoryListResponse, InventoryUpsertRequest

ZERO = Decimal("0")

_UNIT_ALIASES: dict[str, str] = {
    "kilo": "kg", "kilogram": "kg", "kilograms": "kg",
    "gram": "g", "grams": "g",
    "litre": "litre", "liter": "litre", "ltr": "litre",
    "pieces": "piece", "pcs": "piece", "pc": "piece",
    "dozen": "dozen", "dz": "dozen",
    "packet": "packet", "pack": "packet",
    "bottle": "bottle", "btl": "bottle",
    "box": "box", "strip": "strip",
    "meter": "meter", "metre": "meter",
}


def _norm_product(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _norm_unit(unit: str) -> str:
    u = unit.strip().lower()
    return _UNIT_ALIASES.get(u, u)


def _fuzzy_match(query: str, candidate: str) -> bool:
    q, c = _norm_product(query), _norm_product(candidate)
    return q == c or q in c or c in q


# ── Public DB functions (called by AI tool executor) ─────────────────────────

async def get_stock(db: AsyncSession, user_id: int, product_name: str) -> dict:
    result = await db.execute(
        select(Inventory).where(Inventory.user_id == user_id)
    )
    all_items = result.scalars().all()

    matches = [i for i in all_items if _fuzzy_match(product_name, i.product_name)]
    if not matches:
        return {"found": False, "product_name": product_name, "message": f"'{product_name}' inventory mein nahi mila"}

    item = matches[0]
    return {
        "found": True,
        "product_name": item.product_name,
        "quantity": float(item.quantity),
        "unit": item.unit,
        "last_purchase_price": float(item.last_purchase_price) if item.last_purchase_price else None,
        "last_sale_price": float(item.last_sale_price) if item.last_sale_price else None,
        "updated_at": item.updated_at.strftime("%Y-%m-%d %H:%M") if item.updated_at else None,
    }


async def get_customer_balance(db: AsyncSession, user_id: int, customer_name: str) -> dict:
    result = await db.execute(
        select(Customer).where(Customer.user_id == user_id)
    )
    all_customers = result.scalars().all()

    matches = [c for c in all_customers if _fuzzy_match(customer_name, c.name)]
    if not matches:
        return {"found": False, "customer_name": customer_name, "message": f"'{customer_name}' customer nahi mila"}

    customer = matches[0]
    return {
        "found": True,
        "customer_name": customer.name,
        "pending": float(customer.pending),
        "total_sale": float(customer.total_sale),
        "total_received": float(customer.total_received),
    }


async def get_recent_price(db: AsyncSession, user_id: int, product_name: str) -> dict:
    result = await db.execute(
        select(Transaction)
        .where(Transaction.user_id == user_id, Transaction.type.in_(["sale", "purchase"]))
        .order_by(Transaction.created_at.desc())
        .limit(50)
    )
    txs = result.scalars().all()

    for tx in txs:
        for item in tx.items or []:
            if isinstance(item, dict) and _fuzzy_match(product_name, item.get("name", "")):
                rate = item.get("rate_per_unit")
                if rate:
                    return {
                        "found": True,
                        "product_name": item["name"],
                        "rate": float(rate),
                        "unit": item.get("unit", ""),
                        "transaction_type": tx.type,
                        "date": tx.created_at.strftime("%Y-%m-%d"),
                    }

    return {"found": False, "product_name": product_name, "message": f"'{product_name}' ka koi recent price nahi mila"}


# ── Stock update (called from transaction_service) ───────────────────────────

async def adjust_stock(
    db: AsyncSession,
    user_id: int,
    product_name: str,
    quantity_delta: Decimal,
    unit: str,
    purchase_price: Decimal | None = None,
    sale_price: Decimal | None = None,
) -> None:
    norm_name = _norm_product(product_name)
    norm_unit = _norm_unit(unit)

    result = await db.execute(
        select(Inventory).where(Inventory.user_id == user_id)
    )
    all_items = result.scalars().all()
    existing = next((i for i in all_items if _fuzzy_match(norm_name, i.product_name)), None)

    if existing:
        existing.quantity = max(existing.quantity + quantity_delta, ZERO)
        if purchase_price is not None:
            existing.last_purchase_price = purchase_price
        if sale_price is not None:
            existing.last_sale_price = sale_price
    else:
        db.add(Inventory(
            user_id=user_id,
            product_name=norm_name,
            quantity=max(quantity_delta, ZERO),
            unit=norm_unit,
            last_purchase_price=purchase_price,
            last_sale_price=sale_price,
        ))


# ── CRUD for API ─────────────────────────────────────────────────────────────

async def list_inventory(db: AsyncSession, user_id: int) -> InventoryListResponse:
    result = await db.execute(
        select(Inventory)
        .where(Inventory.user_id == user_id)
        .order_by(Inventory.product_name)
    )
    items = result.scalars().all()
    return InventoryListResponse(items=[
        InventoryItemResponse(
            id=i.id,
            product_name=i.product_name,
            quantity=float(i.quantity),
            unit=i.unit,
            last_purchase_price=float(i.last_purchase_price) if i.last_purchase_price else None,
            last_sale_price=float(i.last_sale_price) if i.last_sale_price else None,
        )
        for i in items
    ])


async def upsert_inventory(
    db: AsyncSession,
    user_id: int,
    payload: InventoryUpsertRequest,
) -> InventoryItemResponse:
    norm_name = _norm_product(payload.product_name)
    result = await db.execute(
        select(Inventory).where(Inventory.user_id == user_id)
    )
    all_items = result.scalars().all()
    existing = next((i for i in all_items if _fuzzy_match(norm_name, i.product_name)), None)

    if existing:
        existing.quantity = Decimal(str(payload.quantity))
        existing.unit = _norm_unit(payload.unit)
        if payload.last_purchase_price is not None:
            existing.last_purchase_price = Decimal(str(payload.last_purchase_price))
        await db.flush()
        item = existing
    else:
        item = Inventory(
            user_id=user_id,
            product_name=norm_name,
            quantity=Decimal(str(payload.quantity)),
            unit=_norm_unit(payload.unit),
            last_purchase_price=Decimal(str(payload.last_purchase_price)) if payload.last_purchase_price else None,
        )
        db.add(item)
        await db.flush()

    return InventoryItemResponse(
        id=item.id,
        product_name=item.product_name,
        quantity=float(item.quantity),
        unit=item.unit,
        last_purchase_price=float(item.last_purchase_price) if item.last_purchase_price else None,
        last_sale_price=float(item.last_sale_price) if item.last_sale_price else None,
    )


async def delete_inventory_item(db: AsyncSession, user_id: int, item_id: int) -> bool:
    result = await db.execute(
        select(Inventory).where(Inventory.id == item_id, Inventory.user_id == user_id)
    )
    item = result.scalar_one_or_none()
    if item is None:
        return False
    await db.delete(item)
    return True
