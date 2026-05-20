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


def _match_score(query: str, candidate: str) -> float:
    """Return a [0, 1] similarity score. 1.0 = exact match."""
    q, c = _norm_product(query), _norm_product(candidate)
    if q == c:
        return 1.0
    q_words = set(q.split())
    c_words = set(c.split())
    if q_words == c_words:
        return 0.95
    # All query words present in candidate and query is ≥ 60% as long as candidate
    if q_words.issubset(c_words) and len(q) >= len(c) * 0.6:
        return 0.85
    # Candidate words all present in query (e.g. query "basmati rice 1kg" ↔ "basmati rice")
    if c_words.issubset(q_words):
        return 0.8
    # Single-word query: the word appears as a standalone word in candidate
    if len(q_words) == 1 and q in c_words:
        return 0.7
    # Common words exist (weak)
    common = q_words & c_words
    if common:
        return 0.3 * len(common) / max(len(q_words), len(c_words))
    return 0.0


def _fuzzy_match(query: str, candidate: str) -> bool:
    """Legacy helper — kept for adjust_stock where a single best-match is acceptable."""
    return _match_score(query, candidate) >= 0.7


# ── Public DB functions (called by AI tool executor) ─────────────────────────

async def get_stock(db: AsyncSession, user_id: int, product_name: str) -> dict:
    result = await db.execute(
        select(Inventory).where(Inventory.user_id == user_id)
    )
    all_items = result.scalars().all()

    scored = sorted(
        [(i, _match_score(product_name, i.product_name)) for i in all_items],
        key=lambda x: x[1], reverse=True,
    )
    # Keep only candidates with score >= 0.7
    candidates = [(i, s) for i, s in scored if s >= 0.7]

    if not candidates:
        return {"found": False, "product_name": product_name, "message": f"'{product_name}' inventory mein nahi mila"}

    # Exact or near-exact single match → return directly
    if len(candidates) == 1 or candidates[0][1] >= 0.95:
        item = candidates[0][0]
        return {
            "found": True,
            "product_name": item.product_name,
            "quantity": float(item.quantity),
            "unit": item.unit,
            "last_purchase_price": float(item.last_purchase_price) if item.last_purchase_price else None,
            "last_sale_price": float(item.last_sale_price) if item.last_sale_price else None,
            "updated_at": item.updated_at.strftime("%Y-%m-%d %H:%M") if item.updated_at else None,
        }

    # Multiple matches — return all names so AI can ask the user
    names = [i.product_name for i, _ in candidates[:5]]
    return {
        "found": False,
        "ambiguous": True,
        "product_name": product_name,
        "candidates": names,
        "message": f"Multiple products found for '{product_name}': {', '.join(names)}. Please clarify which one.",
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
        .limit(100)
    )
    txs = result.scalars().all()

    # Collect all distinct matches with their best score
    seen: dict[str, tuple[float, dict]] = {}  # product_name → (score, data)
    for tx in txs:
        for item in tx.items or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "")
            rate = item.get("rate_per_unit")
            if not name or not rate:
                continue
            score = _match_score(product_name, name)
            if score < 0.7:
                continue
            key = _norm_product(name)
            if key not in seen or score > seen[key][0]:
                seen[key] = (score, {
                    "found": True,
                    "product_name": name,
                    "rate": float(rate),
                    "unit": item.get("unit", ""),
                    "transaction_type": tx.type,
                    "date": tx.created_at.strftime("%Y-%m-%d"),
                    "score": score,
                })

    if not seen:
        return {"found": False, "product_name": product_name, "message": f"'{product_name}' ka koi recent price nahi mila"}

    best = max(seen.values(), key=lambda x: x[0])
    best_score, best_data = best

    # Exact or near-exact single match
    high_conf = [v for s, v in seen.values() if s >= 0.85]
    if len(high_conf) == 1 or best_score >= 0.95:
        return best_data

    # Multiple similar products → return candidates list
    candidates = sorted(seen.values(), key=lambda x: x[0], reverse=True)
    names = [v["product_name"] for _, v in candidates[:5]]
    return {
        "found": False,
        "ambiguous": True,
        "product_name": product_name,
        "candidates": names,
        "message": f"Multiple products found for '{product_name}': {', '.join(names)}. Please clarify which one.",
    }


async def search_inventory(db: AsyncSession, user_id: int, query: str) -> list[InventoryItemResponse]:
    """Returns inventory items whose name contains the query string (case-insensitive)."""
    norm_q = _norm_product(query)
    result = await db.execute(
        select(Inventory).where(Inventory.user_id == user_id).order_by(Inventory.product_name)
    )
    items = result.scalars().all()
    matched = [i for i in items if norm_q in _norm_product(i.product_name)]
    return [
        InventoryItemResponse(
            id=i.id,
            product_name=i.product_name,
            quantity=float(i.quantity),
            unit=i.unit,
            last_purchase_price=float(i.last_purchase_price) if i.last_purchase_price else None,
            last_sale_price=float(i.last_sale_price) if i.last_sale_price else None,
        )
        for i in matched
    ]


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
        if payload.last_sale_price is not None:
            existing.last_sale_price = Decimal(str(payload.last_sale_price))
        await db.flush()
        item = existing
    else:
        item = Inventory(
            user_id=user_id,
            product_name=norm_name,
            quantity=Decimal(str(payload.quantity)),
            unit=_norm_unit(payload.unit),
            last_purchase_price=Decimal(str(payload.last_purchase_price)) if payload.last_purchase_price else None,
            last_sale_price=Decimal(str(payload.last_sale_price)) if payload.last_sale_price else None,
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
