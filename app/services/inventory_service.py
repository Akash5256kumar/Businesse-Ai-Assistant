from __future__ import annotations

import re
from decimal import Decimal
from difflib import SequenceMatcher

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


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())

# Keep legacy alias used in other modules
_norm_product = _norm


def _norm_unit(unit: str) -> str:
    u = unit.strip().lower()
    return _UNIT_ALIASES.get(u, u)


def _char_sim(a: str, b: str) -> float:
    """Character-level similarity using difflib (handles typos like panner→paneer)."""
    return SequenceMatcher(None, a, b).ratio()


def _match_score(query: str, candidate: str) -> float:
    """
    Return a [0, 1] similarity score between query and candidate strings.
    Handles:
      - Exact / word-set matches
      - Subset / superset word matches
      - Character-level typo correction (panner → paneer, chawell → chawal)
    """
    q, c = _norm(query), _norm(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0

    q_words = set(q.split())
    c_words = set(c.split())

    if q_words == c_words:
        return 0.95
    # All query words in candidate and query is ≥ 60 % as long as candidate
    if q_words.issubset(c_words) and len(q) >= len(c) * 0.6:
        return 0.85
    # All candidate words in query (e.g. "basmati rice 1kg" ↔ "basmati rice")
    if c_words.issubset(q_words):
        return 0.80
    # Single-word query matches a whole word in candidate
    if len(q_words) == 1 and q in c_words:
        return 0.70

    # ── Character-level fuzzy (typo tolerance) ────────────────────────────────
    q_list = q.split()
    c_list = c.split()
    if q_list and c_list:
        # Each query word: find best matching candidate word by char similarity
        word_sims = [max(_char_sim(qw, cw) for cw in c_list) for qw in q_list]
        avg = sum(word_sims) / len(word_sims)
        if avg >= 0.85:
            # Very high char similarity (e.g. "basmti"↔"basmati", "sona masuri"↔"sona masoori")
            # High char similarity — score in 0.80–0.82 range, below 0.90 auto-threshold → AMBIGUOUS.
            return 0.80 + (avg - 0.85) * 0.15  # 0.85→0.80, 1.0→0.8225
        if avg >= 0.75:
            # Moderate similarity → score stays below auto-select; user picks from dropdown
            return 0.65 + avg * 0.07  # 0.75→0.7025, 1.0→0.72

    # Partial word overlap (weak signal)
    common = q_words & c_words
    if common:
        return 0.3 * len(common) / max(len(q_words), len(c_words))
    return 0.0


def _fuzzy_match(query: str, candidate: str) -> bool:
    """True when score ≥ 0.70 (used by adjust_stock and upsert lookups)."""
    return _match_score(query, candidate) >= 0.70


def _item_score(query: str, item: Inventory) -> float:
    """Best score considering product_name AND category (if set)."""
    name_score = _match_score(query, item.product_name)
    if item.category:
        cat_score = _match_score(query, item.category)
        return max(name_score, cat_score)
    return name_score


def _to_response(item: Inventory) -> InventoryItemResponse:
    return InventoryItemResponse(
        id=item.id,
        category=item.category,
        product_name=item.product_name,
        quantity=float(item.quantity),
        unit=item.unit,
        last_purchase_price=float(item.last_purchase_price) if item.last_purchase_price else None,
        last_sale_price=float(item.last_sale_price) if item.last_sale_price else None,
    )


# ── Public DB functions (called by AI tool executor) ─────────────────────────

async def get_stock(db: AsyncSession, user_id: int, product_name: str) -> dict:
    result = await db.execute(select(Inventory).where(Inventory.user_id == user_id))
    all_items = result.scalars().all()

    scored = sorted(
        [(i, _item_score(product_name, i)) for i in all_items],
        key=lambda x: x[1], reverse=True,
    )
    candidates = [(i, s) for i, s in scored if s >= 0.70]

    if not candidates:
        return {"found": False, "product_name": product_name,
                "message": f"'{product_name}' inventory mein nahi mila"}

    if len(candidates) == 1 or candidates[0][1] >= 0.95:
        item = candidates[0][0]
        return {
            "found": True,
            "category": item.category,
            "product_name": item.product_name,
            "quantity": float(item.quantity),
            "unit": item.unit,
            "last_purchase_price": float(item.last_purchase_price) if item.last_purchase_price else None,
            "last_sale_price": float(item.last_sale_price) if item.last_sale_price else None,
            "updated_at": item.updated_at.strftime("%Y-%m-%d %H:%M") if item.updated_at else None,
        }

    names = [i.product_name for i, _ in candidates[:5]]
    return {
        "found": False,
        "ambiguous": True,
        "product_name": product_name,
        "candidates": names,
        "message": f"Multiple products found for '{product_name}': {', '.join(names)}. Please clarify.",
    }


async def get_customer_balance(db: AsyncSession, user_id: int, customer_name: str) -> dict:
    result = await db.execute(select(Customer).where(Customer.user_id == user_id))
    all_customers = result.scalars().all()

    matches = [c for c in all_customers if _fuzzy_match(customer_name, c.name)]
    if not matches:
        return {"found": False, "customer_name": customer_name,
                "message": f"'{customer_name}' customer nahi mila"}

    customer = matches[0]
    return {
        "found": True,
        "customer_name": customer.name,
        "pending": float(customer.pending),
        "total_sale": float(customer.total_sale),
        "total_received": float(customer.total_received),
    }


async def get_recent_price(db: AsyncSession, user_id: int, product_name: str) -> dict:
    """
    Find the best available price for a product.
    Auto-select threshold: score >= 0.75 → confident match, use without asking.
    Score 0.70-0.74 → ambiguous, return top-3 candidates for dropdown (never ask in chat).
    Priority:
      1. Inventory table — last_sale_price (preferred for sales), then last_purchase_price.
      2. Past transactions — most recent rate_per_unit from sale/purchase items.
         Tie-break by frequency (most-ordered product wins).
    """
    _AUTO_THRESHOLD = 0.90   # ≥ 0.90 → high-confidence auto-proceed; 0.70–0.89 → AMBIGUOUS dropdown
    _FUZZY_THRESHOLD = 0.70

    # ── Source 1: Inventory table ─────────────────────────────────────────────
    inv_result = await db.execute(select(Inventory).where(Inventory.user_id == user_id))
    all_inv = inv_result.scalars().all()

    scored_inv = sorted(
        [(i, _item_score(product_name, i)) for i in all_inv],
        key=lambda x: x[1], reverse=True,
    )
    candidates_inv = [(i, s) for i, s in scored_inv if s >= _FUZZY_THRESHOLD]

    if candidates_inv:
        best_inv, best_score = candidates_inv[0]
        if best_score >= _AUTO_THRESHOLD:
            # Confident match — auto-select, no clarification needed
            price = best_inv.last_sale_price or best_inv.last_purchase_price
            if price:
                return {
                    "found": True,
                    "category": best_inv.category,
                    "product_name": best_inv.product_name,
                    "rate": float(price),
                    "unit": best_inv.unit or "",
                    "source": "inventory",
                    "score": best_score,
                }
            # Inventory entry exists but no price stored — fall through to transactions
        else:
            # Below auto-select threshold → ambiguous; user picks from card dropdown
            names = [i.product_name for i, _ in candidates_inv[:3]]
            return {
                "found": False,
                "ambiguous": True,
                "product_name": product_name,
                "candidates": names,
                "message": f"Multiple products found for '{product_name}': {', '.join(names)}.",
            }

    # ── Source 2: Past transactions ───────────────────────────────────────────
    tx_result = await db.execute(
        select(Transaction)
        .where(Transaction.user_id == user_id, Transaction.type.in_(["sale", "purchase"]))
        .order_by(Transaction.created_at.desc())
        .limit(200)
    )
    txs = tx_result.scalars().all()

    # Track best rate AND frequency for each normalised product name
    seen: dict[str, tuple[float, int, dict]] = {}  # key → (score, count, data)
    for tx in txs:
        for item in tx.items or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "")
            rate = item.get("rate_per_unit")
            if not name or not rate:
                continue
            score = _match_score(product_name, name)
            if score < _FUZZY_THRESHOLD:
                continue
            key = _norm(name)
            prev_score, prev_count, prev_data = seen.get(key, (0.0, 0, {}))
            count = prev_count + 1
            if score >= prev_score:
                seen[key] = (score, count, {
                    "found": True,
                    "product_name": name,
                    "rate": float(rate),
                    "unit": item.get("unit", ""),
                    "source": "transactions",
                    "transaction_type": tx.type,
                    "date": tx.created_at.strftime("%Y-%m-%d"),
                    "score": score,
                })
            else:
                seen[key] = (prev_score, count, prev_data)

    if not seen:
        return {
            "found": False,
            "product_name": product_name,
            "message": f"'{product_name}' ka koi price nahi mila. Rate null rakha — user edit screen mein set kar sakta hai.",
        }

    # Sort by score desc, then by frequency desc (most-ordered wins on tie)
    ranked = sorted(seen.values(), key=lambda x: (x[0], x[1]), reverse=True)
    best_score, best_count, best_data = ranked[0]

    if best_score >= _AUTO_THRESHOLD:
        return best_data

    # Best match below auto-select threshold → ambiguous
    names = [v["product_name"] for _, _, v in ranked[:3]]
    return {
        "found": False,
        "ambiguous": True,
        "product_name": product_name,
        "candidates": names,
        "message": f"Multiple products found for '{product_name}': {', '.join(names)}.",
    }


async def find_product_catalog_matches(
    db: AsyncSession,
    user_id: int,
    product_name: str,
    top_k: int = 3,
) -> dict:
    """
    Step 2: RAG fuzzy matching of a product name against the shop's inventory catalog.

    Uses character-level + word-set similarity (the same _match_score used elsewhere)
    as the fuzzy-string matching component.  Returns top-k matches with confidence scores.

    Thresholds:
      ≥ 0.90 → high confidence, safe to auto-proceed
      0.50–0.89 → ambiguous, needs_clarification = True
      < 0.50 → not found, product_not_found = True
    """
    inv_result = await db.execute(select(Inventory).where(Inventory.user_id == user_id))
    all_inv = inv_result.scalars().all()

    if not all_inv:
        return {
            "top_match_confidence": 0.0,
            "matches": [],
            "needs_clarification": False,
            "product_not_found": True,
        }

    scored = sorted(
        [(i, _item_score(product_name, i)) for i in all_inv],
        key=lambda x: x[1],
        reverse=True,
    )

    matches = [
        {
            "product_name": inv.product_name,
            "confidence": round(score, 3),
            "last_sale_price": float(inv.last_sale_price) if inv.last_sale_price else None,
            "last_purchase_price": float(inv.last_purchase_price) if inv.last_purchase_price else None,
            "unit": inv.unit,
        }
        for inv, score in scored[:top_k]
        if score > 0.0
    ]

    top_confidence = matches[0]["confidence"] if matches else 0.0

    return {
        "top_match_confidence": top_confidence,
        "matches": matches,
        "needs_clarification": 0.50 <= top_confidence < 0.90,
        "product_not_found": top_confidence < 0.50,
    }


async def search_inventory(db: AsyncSession, user_id: int, query: str) -> list[InventoryItemResponse]:
    """
    Returns inventory items that match query against product_name OR category.
    Uses fuzzy scoring so typos still find the right product.
    """
    result = await db.execute(
        select(Inventory).where(Inventory.user_id == user_id).order_by(Inventory.product_name)
    )
    items = result.scalars().all()

    if not query.strip():
        return [_to_response(i) for i in items]

    norm_q = _norm(query)
    matched = []
    for i in items:
        # Fast substring check first (common case, free)
        name_match = norm_q in _norm(i.product_name)
        cat_match = i.category and norm_q in _norm(i.category)
        if name_match or cat_match:
            matched.append(i)
            continue
        # Fuzzy fallback for typos
        if _item_score(query, i) >= 0.70:
            matched.append(i)

    return [_to_response(i) for i in matched]


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
    norm_name = _norm(product_name)
    norm_unit = _norm_unit(unit)

    result = await db.execute(select(Inventory).where(Inventory.user_id == user_id))
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
        select(Inventory).where(Inventory.user_id == user_id).order_by(Inventory.product_name)
    )
    items = result.scalars().all()
    return InventoryListResponse(items=[_to_response(i) for i in items])


async def upsert_inventory(
    db: AsyncSession,
    user_id: int,
    payload: InventoryUpsertRequest,
) -> InventoryItemResponse:
    norm_name = _norm(payload.product_name)
    result = await db.execute(select(Inventory).where(Inventory.user_id == user_id))
    all_items = result.scalars().all()
    existing = next((i for i in all_items if _fuzzy_match(norm_name, i.product_name)), None)

    if existing:
        existing.quantity = Decimal(str(payload.quantity))
        existing.unit = _norm_unit(payload.unit)
        if payload.category is not None:
            existing.category = payload.category
        if payload.last_purchase_price is not None:
            existing.last_purchase_price = Decimal(str(payload.last_purchase_price))
        if payload.last_sale_price is not None:
            existing.last_sale_price = Decimal(str(payload.last_sale_price))
        await db.flush()
        item = existing
    else:
        item = Inventory(
            user_id=user_id,
            category=payload.category,
            product_name=norm_name,
            quantity=Decimal(str(payload.quantity)),
            unit=_norm_unit(payload.unit),
            last_purchase_price=Decimal(str(payload.last_purchase_price)) if payload.last_purchase_price else None,
            last_sale_price=Decimal(str(payload.last_sale_price)) if payload.last_sale_price else None,
        )
        db.add(item)
        await db.flush()

    return _to_response(item)


async def delete_inventory_item(db: AsyncSession, user_id: int, item_id: int) -> bool:
    result = await db.execute(
        select(Inventory).where(Inventory.id == item_id, Inventory.user_id == user_id)
    )
    item = result.scalar_one_or_none()
    if item is None:
        return False
    await db.delete(item)
    return True
