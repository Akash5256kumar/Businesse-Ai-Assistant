from __future__ import annotations

import csv
import io
import re
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.inventory import Inventory
from app.models.transaction import Transaction
from app.schemas.inventory import (
    ImportRowResult,
    ImportSummaryResponse,
    InventoryItemResponse,
    InventoryListResponse,
    InventoryUpsertRequest,
)

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

_GRAIN_SUFFIXES = {"rice", "chawal", "chaawal", "chawl", "grain"}


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


def _words(name: str) -> list[str]:
    return [part for part in _norm(name).split() if part]


def _core_words(name: str) -> list[str]:
    words = _words(name)
    while words and words[-1] in _GRAIN_SUFFIXES:
        words.pop()
    return words or _words(name)


def _numeric_tokens(words: list[str]) -> tuple[str, ...]:
    return tuple(word for word in words if any(ch.isdigit() for ch in word))


def _has_numeric_conflict(query_words: list[str], candidate_words: list[str]) -> bool:
    q_nums = _numeric_tokens(query_words)
    c_nums = _numeric_tokens(candidate_words)
    return bool(q_nums or c_nums) and q_nums != c_nums


def _identity_match_score(query: str, candidate: str) -> float:
    """
    Conservative identity matcher used before mutating data or auto-selecting a SKU.

    A match is considered safe only when:
      - names are equal after normalization, or
      - they differ only by generic grain suffixes ("rice", "chawal"), or
      - they are the same token-by-token with very small typos.
    """
    q_words = _core_words(query)
    c_words = _core_words(candidate)
    if not q_words or not c_words:
        return 0.0
    if q_words == c_words:
        return 1.0
    if _has_numeric_conflict(q_words, c_words):
        return 0.0
    if len(q_words) != len(c_words):
        return 0.0

    sims: list[float] = []
    for q_word, c_word in zip(q_words, c_words):
        if q_word == c_word:
            sims.append(1.0)
            continue
        if any(ch.isdigit() for ch in q_word) or any(ch.isdigit() for ch in c_word):
            return 0.0
        sims.append(_char_sim(q_word, c_word))

    avg = sum(sims) / len(sims)
    return 0.93 if sims and min(sims) >= 0.80 and avg >= 0.92 else 0.0


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

    q_core = _core_words(query)
    c_core = _core_words(candidate)
    if not q_core or not c_core:
        return 0.0
    if q_core == c_core:
        return 0.95

    q_words = set(q_core)
    c_words = set(c_core)
    numeric_conflict = _has_numeric_conflict(q_core, c_core)

    if q_words == c_words:
        return 0.95
    # One side is a less-specific variant of the other ("govind bhog" vs "govind bhog old").
    # Keep this below the auto-select threshold so the caller can surface ambiguity safely.
    if q_words.issubset(c_words) and len(q_words) < len(c_words):
        return 0.60
    if c_words.issubset(q_words):
        return 0.60
    # Single-word query matches a whole word in candidate
    if len(q_words) == 1 and q in c_words:
        return 0.70
    if numeric_conflict:
        common = q_words & c_words
        if common:
            return 0.60 * len(common) / min(len(q_words), len(c_words))
        return 0.0

    # ── Character-level fuzzy (typo tolerance) ────────────────────────────────
    q_list = q_core
    c_list = c_core
    if q_list and c_list:
        # Each query word: find best matching candidate word by char similarity
        word_sims = [max(_char_sim(qw, cw) for cw in c_list) for qw in q_list]
        avg = sum(word_sims) / len(word_sims)
        if avg >= 0.85:
            # Very high char similarity (e.g. "basmti"↔"basmati", "sona masuri"↔"sona masoori")
            # High char similarity — score in 0.80–0.82 range, below 0.90 auto-threshold → AMBIGUOUS.
            return 0.80 + (avg - 0.85) * 0.15  # 0.85→0.80, 1.0→0.8225
        if avg >= 0.70:
            # Moderate similarity → AMBIGUOUS (dropdown); floor at 0.70 ensures fuzzy threshold cleared
            return 0.70 + (avg - 0.70) * 0.20  # 0.70→0.70, 0.85→0.73, 1.0→0.76

    # Partial word overlap (weak signal)
    common = q_words & c_words
    if common:
        return 0.3 * len(common) / max(len(q_words), len(c_words))
    return 0.0


def _fuzzy_match(query: str, candidate: str) -> bool:
    """True when score ≥ 0.70 (used by adjust_stock and upsert lookups)."""
    return _match_score(query, candidate) >= 0.70


def _identity_matches(query: str, items: list[Inventory]) -> list[tuple[Inventory, float]]:
    scored = sorted(
        [
            (item, _identity_match_score(query, item.product_name))
            for item in items
        ],
        key=lambda pair: pair[1],
        reverse=True,
    )
    return [(item, score) for item, score in scored if score > 0.0]


def _fuzzy_item_matches(
    query: str,
    items: list[Inventory],
    threshold: float,
) -> list[tuple[Inventory, float]]:
    scored = sorted(
        [(item, _item_score(query, item)) for item in items],
        key=lambda pair: pair[1],
        reverse=True,
    )
    return [(item, score) for item, score in scored if score >= threshold]


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

    exact_matches = _identity_matches(product_name, all_items)
    if exact_matches:
        item = exact_matches[0][0]
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

    candidates = _fuzzy_item_matches(product_name, all_items, 0.60)
    if not candidates:
        return {"found": False, "product_name": product_name,
                "message": f"'{product_name}' inventory mein nahi mila"}

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
    _FUZZY_THRESHOLD = 0.60

    # ── Source 1: Inventory table ─────────────────────────────────────────────
    inv_result = await db.execute(select(Inventory).where(Inventory.user_id == user_id))
    all_inv = inv_result.scalars().all()

    exact_inv = _identity_matches(product_name, all_inv)
    if exact_inv:
        best_inv, best_score = exact_inv[0]
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

    candidates_inv = _fuzzy_item_matches(product_name, all_inv, _FUZZY_THRESHOLD)
    if candidates_inv:
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
    exact_seen: dict[str, tuple[float, int, dict]] = {}
    fuzzy_seen: dict[str, tuple[float, int, dict]] = {}
    for tx in txs:
        for item in tx.items or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "")
            rate = item.get("rate_per_unit")
            if not name or not rate:
                continue
            identity_score = _identity_match_score(product_name, name)
            score = _match_score(product_name, name)
            if identity_score <= 0.0 and score < _FUZZY_THRESHOLD:
                continue
            key = _norm(name)
            target = exact_seen if identity_score > 0.0 else fuzzy_seen
            chosen_score = identity_score if identity_score > 0.0 else score
            prev_score, prev_count, prev_data = target.get(key, (0.0, 0, {}))
            count = prev_count + 1
            if chosen_score >= prev_score:
                target[key] = (chosen_score, count, {
                    "found": True,
                    "product_name": name,
                    "rate": float(rate),
                    "unit": item.get("unit", ""),
                    "source": "transactions",
                    "transaction_type": tx.type,
                    "date": tx.created_at.strftime("%Y-%m-%d"),
                    "score": chosen_score,
                })
            else:
                target[key] = (prev_score, count, prev_data)

    if exact_seen:
        ranked_exact = sorted(exact_seen.values(), key=lambda x: (x[0], x[1]), reverse=True)
        _, _, best_data = ranked_exact[0]
        return best_data

    if not fuzzy_seen:
        return {
            "found": False,
            "product_name": product_name,
            "message": f"'{product_name}' ka koi price nahi mila. Rate null rakha — user edit screen mein set kar sakta hai.",
        }

    # Best fuzzy match stays ambiguous — never auto-select without a safe identity match.
    ranked = sorted(fuzzy_seen.values(), key=lambda x: (x[0], x[1]), reverse=True)
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
      ≥ 0.80 → high confidence, safe to auto-proceed
      0.50–0.79 → ambiguous, needs_clarification = True
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

    exact_matches = _identity_matches(product_name, all_inv)
    if exact_matches:
        matches = [
            {
                "product_name": inv.product_name,
                "confidence": round(score, 3),
                "last_sale_price": float(inv.last_sale_price) if inv.last_sale_price else None,
                "last_purchase_price": float(inv.last_purchase_price) if inv.last_purchase_price else None,
                "unit": inv.unit,
            }
            for inv, score in exact_matches[:top_k]
        ]
        top_confidence = matches[0]["confidence"] if matches else 0.0
        return {
            "top_match_confidence": top_confidence,
            "matches": matches,
            "needs_clarification": False,
            "product_not_found": False,
        }

    scored = sorted(
        [(i, min(_item_score(product_name, i), 0.79)) for i in all_inv],
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
        "needs_clarification": 0.50 <= top_confidence < 0.80,
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
    exact_matches = _identity_matches(norm_name, all_items)
    existing = exact_matches[0][0] if exact_matches else None

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
    result = await db.execute(
        select(Inventory).where(
            Inventory.user_id == user_id,
            Inventory.product_name == norm_name,
        )
    )
    existing = result.scalar_one_or_none()

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


# ── Excel / CSV bulk import ───────────────────────────────────────────────────

_COLUMN_ALIASES: dict[str, str] = {
    # product_name
    "product": "product_name",
    "item": "product_name",
    "item name": "product_name",
    "name": "product_name",
    "product name": "product_name",
    # quantity
    "qty": "quantity",
    "stock": "quantity",
    "stock qty": "quantity",
    # unit
    "uom": "unit",
    "unit of measure": "unit",
    # last_purchase_price
    "purchase price": "last_purchase_price",
    "buy price": "last_purchase_price",
    "cost": "last_purchase_price",
    "cost price": "last_purchase_price",
    "purchase_price": "last_purchase_price",
    # last_sale_price
    "sale price": "last_sale_price",
    "sell price": "last_sale_price",
    "selling price": "last_sale_price",
    "mrp": "last_sale_price",
    "price": "last_sale_price",
    "sale_price": "last_sale_price",
    # category
    "cat": "category",
    "type": "category",
}


def _canonical_col(raw: str) -> str:
    key = raw.strip().lower()
    return _COLUMN_ALIASES.get(key, key.replace(" ", "_"))


def _safe_decimal(value: str | None) -> Decimal | None:
    if value is None or str(value).strip() in ("", "-", "N/A", "n/a", "NA"):
        return None
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation:
        return None


def _parse_rows_from_csv(content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    raw_headers = reader.fieldnames or []
    canon = {h: _canonical_col(h) for h in raw_headers}
    rows: list[dict] = []
    for row in reader:
        rows.append({canon[k]: v for k, v in row.items()})
    return rows


def _parse_rows_from_xlsx(content: bytes) -> list[dict]:
    import openpyxl  # imported here so it's only loaded when needed
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    all_rows = list(ws.iter_rows(values_only=True))
    if not all_rows:
        return []
    raw_headers = [str(h) if h is not None else "" for h in all_rows[0]]
    canon = [_canonical_col(h) for h in raw_headers]
    rows: list[dict] = []
    for data_row in all_rows[1:]:
        if all(cell is None or str(cell).strip() == "" for cell in data_row):
            continue
        rows.append({canon[i]: (str(data_row[i]) if data_row[i] is not None else "") for i in range(len(canon))})
    wb.close()
    return rows


async def import_inventory_from_file(
    db: AsyncSession,
    user_id: int,
    content: bytes,
    filename: str,
) -> ImportSummaryResponse:
    rows = _parse_rows_from_xlsx(content) if filename.endswith(".xlsx") else _parse_rows_from_csv(content)

    # Pre-load all existing inventory for fast duplicate check
    existing_result = await db.execute(select(Inventory).where(Inventory.user_id == user_id))
    existing_items = {i.product_name: i for i in existing_result.scalars().all()}

    results: list[ImportRowResult] = []
    imported = updated = skipped = 0

    for idx, row in enumerate(rows, start=2):  # row 1 is header
        raw_name = row.get("product_name", "").strip()
        if not raw_name:
            skipped += 1
            results.append(ImportRowResult(row=idx, product_name="(empty)", status="skipped", reason="product_name is empty"))
            continue

        norm_name = _norm(raw_name)
        qty = _safe_decimal(row.get("quantity", "0") or "0") or Decimal("0")
        unit = _norm_unit(row.get("unit", "piece") or "piece")
        category_raw = row.get("category", "")
        category = category_raw.strip().lower() if category_raw and category_raw.strip() else None
        purchase_price = _safe_decimal(row.get("last_purchase_price"))
        sale_price = _safe_decimal(row.get("last_sale_price"))

        if norm_name in existing_items:
            item = existing_items[norm_name]
            item.quantity = qty
            item.unit = unit
            if category is not None:
                item.category = category
            if purchase_price is not None:
                item.last_purchase_price = purchase_price
            if sale_price is not None:
                item.last_sale_price = sale_price
            updated += 1
            results.append(ImportRowResult(row=idx, product_name=raw_name, status="updated"))
        else:
            new_item = Inventory(
                user_id=user_id,
                product_name=norm_name,
                quantity=qty,
                unit=unit,
                category=category,
                last_purchase_price=purchase_price,
                last_sale_price=sale_price,
            )
            db.add(new_item)
            existing_items[norm_name] = new_item
            imported += 1
            results.append(ImportRowResult(row=idx, product_name=raw_name, status="imported"))

    await db.flush()
    return ImportSummaryResponse(
        total_rows=len(rows),
        imported=imported,
        updated=updated,
        skipped=skipped,
        rows=results,
    )
