from __future__ import annotations

import asyncio
import json
import logging
import re
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.business import Business
from app.models.message_log import MessageLog
from app.schemas.chat import (
    AddToInventoryRequest,
    ChatResponse,
    ConfirmTransactionRequest,
    CustomerCandidate,
    CustomerConfirmRequest,
    InventoryActionProduct,
    MurilAnalysisResponse,
    MurilEntityResponse,
    ResponseItem,
    SkipProductRequest,
    TransactionDetail,
    TransactionDraft,
    TransactionDraftItem,
)
from app.services import ai_service, customer_service, inventory_service, transaction_service
from app.services.muril_service import muril_service

_logger = logging.getLogger(__name__)

_VALID_TYPES = {"sale", "payment", "purchase", "expense", "query"}
_HISTORY_LIMIT = 12

# ── Greeting detection ────────────────────────────────────────────────────────

_GREETING_RE = re.compile(
    r"^\s*(hi+|hello+|hey+|helo|namaste|namaskar|sat\s*sri\s*akal|"
    r"kya\s+haal\s*(hai)?|kaise\s+ho|kya\s+chal\s*(raha)?|sab\s+theek|"
    r"good\s+(morning|evening|night|afternoon)|"
    r"thanks?|thank\s+you|dhanyavaad|shukriya|aap\s+kaise)\W*$",
    re.IGNORECASE,
)


def _greeting_reply(msg: str) -> str:
    lower = msg.lower().strip()
    if any(w in lower for w in ["namaste", "namaskar"]):
        return "Namaste! Aaj main aapki kya madad kar sakta hoon?\nSale entry, payment, ya kuch aur?"
    if any(w in lower for w in ["shukriya", "dhanyavaad"]):
        return "Aapka swagat hai! Koi aur kaam ho toh batao."
    if any(w in lower for w in ["thanks", "thank you"]):
        return "You're welcome! Let me know if you need anything else."
    if "good morning" in lower:
        return "Good morning! Aaj ka din accha rahe. Kaise madad kar sakta hoon?"
    if any(w in lower for w in ["good evening", "good night"]):
        return "Good evening! Kaise madad kar sakta hoon aapki?"
    if any(w in lower for w in ["kaise ho", "sab theek", "kya haal", "kya chal"]):
        return "Sab theek hai, shukriya! Batao, aaj kya kaam karna hai?"
    return "Hello! Kaise madad kar sakta hoon? Sale entry, payment, ya kuch aur?"


# ── Helper builders ───────────────────────────────────────────────────────────

def _sale_reply(customer_name: str, amount: Decimal, pending: Decimal, is_credit: bool) -> str:
    base = f"✅ Sale recorded\n💰 ₹{amount:,.0f}\n👤 {customer_name}"
    if is_credit and pending > 0:
        return f"{base}\n⏳ Baaki: ₹{pending:,.0f}"
    return base


def _build_items(raw_items: list) -> list[ResponseItem]:
    return [
        ResponseItem(
            name=item.get("name", ""),
            quantity=float(item.get("quantity", 0)),
            unit=item.get("unit"),
            rate_per_unit=item.get("rate_per_unit"),
            subtotal=float(item.get("subtotal", 0)),
        )
        for item in raw_items
    ]


def _format_muril_analysis(analysis: dict) -> MurilAnalysisResponse | None:
    """Convert the raw muril_service.analyze() dict to the Pydantic response model."""
    if not analysis:
        return None
    return MurilAnalysisResponse(
        detected_language=analysis.get("detected_language", "hi-Latn"),
        intent=analysis.get("intent", "UNCLEAR"),
        intent_confidence=analysis.get("intent_confidence", 0.0),
        entities=[
            MurilEntityResponse(
                type=e["type"], value=e["value"], score=e["score"]
            )
            for e in analysis.get("entities", [])
            if e.get("score", 0) >= 0.70
        ],
        normalized_text=analysis.get("normalized_text", ""),
    )


# ── History helpers ───────────────────────────────────────────────────────────

async def _get_recent_logs(db: AsyncSession, user_id: int) -> list[MessageLog]:
    result = await db.execute(
        select(MessageLog)
        .where(MessageLog.user_id == user_id)
        .order_by(MessageLog.created_at.desc(), MessageLog.id.desc())
        .limit(_HISTORY_LIMIT)
    )
    logs = result.scalars().all()
    return list(reversed(logs))


def _build_history(logs: list[MessageLog]) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    for log in logs:
        history.append({"role": "user", "content": log.user_message})
        if log.ai_response:
            history.append({"role": "assistant", "content": json.dumps(log.ai_response)})
        else:
            history.append({"role": "assistant", "content": log.reply})
    return history


def _get_pending_clarification(logs: list[MessageLog]) -> dict | None:
    if not logs:
        return None
    last_log = logs[-1]
    if not last_log.ai_response:
        return None
    clarification = last_log.ai_response.get("clarification_needed")
    if not clarification:
        return None
    return {
        "previous_user_message": last_log.user_message,
        "assistant_question": clarification,
        # Pass the full AI response so _build_messages can replay it accurately
        # instead of injecting a fake {"transactions": []} that loses partial state.
        "full_ai_response": last_log.ai_response,
    }


# ── Customer candidate helpers ────────────────────────────────────────────────

def _candidate_list(
    customers_with_scores: list[tuple],
) -> list[CustomerCandidate]:
    """
    Accepts either:
      • list[Customer]              — plain list (no MuRIL scores)
      • list[tuple[Customer, float]] — MuRIL-ranked pairs
    """
    result: list[CustomerCandidate] = []
    for item in customers_with_scores:
        if isinstance(item, tuple):
            customer, score = item
            sim = round(score, 4) if score > 0.0 else None
        else:
            customer, sim = item, None
        result.append(
            CustomerCandidate(
                id=customer.id,
                name=customer.name,
                phone=customer.phone,
                pending=float(customer.pending),
                similarity_score=sim,
            )
        )
    return result


# ── Draft helpers ─────────────────────────────────────────────────────────────

def _is_complete_sale(tx: dict) -> bool:
    """
    True when enough data has been collected to trigger the inventory check + confirmation.
    rate_per_unit is not checked here — _validate_sale_items enforces the inventory rule
    immediately after, blocking if any product is not found in the catalog.
    """
    if tx.get("type") != "sale":
        return False
    if not tx.get("customer_name"):
        return False
    items = tx.get("items") or []
    if not items:
        return False
    if tx.get("amount_paid") is None:
        return False
    for item in items:
        if not item.get("name"):
            return False
        if item.get("quantity") is None:
            return False
    return True


# ── Bug 1: Devanagari scrub (defense-in-depth for hardcoded reply strings) ────

_DEVANAGARI_SCRUB_RE = re.compile(r"[ऀ-ॿ]+")


def _scrub(text: str) -> str:
    """Remove any Devanagari characters that slipped into a reply string."""
    return _DEVANAGARI_SCRUB_RE.sub("", text).strip()


# ── Bug 2 Step 3: Inventory validation ───────────────────────────────────────

async def _validate_sale_items(
    db: AsyncSession,
    user_id: int,
    items: list[dict],
) -> list[tuple[dict, str, dict]]:
    """
    For each sale item, determine inventory status.
    Returns list of (item, status, catalog_data) where:
      status       — 'found' | 'ambiguous' | 'not_found'
      catalog_data — dict from find_product_catalog_matches (empty {} for fast-path cases)

    Fast-path: if the AI already marked price_source as 'not_found' or 'ambiguous',
    trust that sentinel and skip the extra catalog DB call for that item.
    Otherwise, verify via find_product_catalog_matches (≥ 0.80 threshold).
    """
    results: list[tuple[dict, str, dict]] = []
    for item in items:
        name = (item.get("name") or "").strip()
        if not name:
            continue

        price_source = item.get("price_source", "")

        # Fast-path: trust the AI's sentinel set during get_recent_price tool call
        if price_source == "not_found":
            results.append((item, "not_found", {}))
            continue
        if price_source == "ambiguous":
            results.append((item, "ambiguous", {}))
            continue

        # Authoritative catalog check for all other cases (including price_source "inventory"
        # or "user" where we still need to confirm the product actually exists in DB)
        catalog = await inventory_service.find_product_catalog_matches(db, user_id, name)
        top_conf = catalog.get("top_match_confidence", 0.0)
        if top_conf >= 0.80:
            results.append((item, "found", catalog))
        elif top_conf >= 0.50:
            results.append((item, "ambiguous", catalog))
        else:
            results.append((item, "not_found", catalog))
    return results


# ── Bug 2 Step 4: Order summary for confirmation ──────────────────────────────

def _build_order_summary(tx: dict) -> str:
    """
    Step 4 confirmation message.
    Format: 'Order Summary: [Product] — Rs[price]/[unit] x [qty] = Rs[total]. Confirm karo?'
    """
    customer = tx.get("customer_name") or "Customer"
    items = tx.get("items") or []
    total = float(tx.get("total_amount") or 0)
    paid = float(tx.get("amount_paid") or 0)
    pending = max(round(total - paid, 2), 0.0)

    lines: list[str] = [f"Order Summary — {customer}:"]
    for item in items:
        name = item.get("name") or ""
        qty = item.get("quantity") or 0
        unit = item.get("unit") or "piece"
        rate = item.get("rate_per_unit")
        subtotal = float(item.get("subtotal") or 0)
        if rate is not None:
            lines.append(f"  {name}: Rs{rate}/{unit} x {qty} = Rs{subtotal:.0f}")
        else:
            lines.append(f"  {name}: {qty} {unit} (rate pending)")

    lines.append(f"Total: Rs{total:.0f}")
    if pending > 0:
        lines.append(f"Baaki: Rs{pending:.0f}")
    lines.append("Confirm karo?")
    return "\n".join(lines)


def _build_transaction_draft(tx: dict) -> TransactionDraft:
    items = [
        TransactionDraftItem(
            name=item.get("name", ""),
            quantity=float(item.get("quantity") or 0),
            unit=item.get("unit"),
            rate_per_unit=float(item.get("rate_per_unit") or 0),
            subtotal=float(item.get("subtotal") or 0),
            price_source=item.get("price_source", "user"),
        )
        for item in (tx.get("items") or [])
        if item.get("name")
    ]
    total = float(tx.get("total_amount") or 0)
    paid = float(tx.get("amount_paid") or 0)
    pending = max(round(total - paid, 2), 0.0)
    return TransactionDraft(
        type=tx.get("type", "sale"),
        customer_name=tx.get("customer_name"),
        items=items,
        total_amount=total,
        amount_paid=paid,
        pending_amount=pending,
        is_credit=pending > 0,
        note=tx.get("note"),
    )


# ── Bug 4: Server-side DB price verification ─────────────────────────────────

def _apply_db_prices(
    tx: dict,
    item_statuses: list[tuple[dict, str, dict]],
) -> dict:
    """
    Rebuild sale items with DB-confirmed names and prices (Bug 4).

    - found items: replace name with DB product_name and rate with DB last_sale_price
      (fallback: last_purchase_price). Recalculate subtotal.
    - ambiguous items: kept as-is (null rate, excluded from total).
    - not_found items: should not appear here (caller handles Bug 3 first).

    Fresh total = SUM of found-item subtotals only (per spec: exclude unconfirmed lines).
    If amount_paid == original total (full-payment indicator), update to fresh total.
    """
    new_items: list[dict] = []
    for item, status, catalog_data in item_statuses:
        if status == "found" and catalog_data.get("matches"):
            best = catalog_data["matches"][0]
            db_name = best["product_name"]
            db_price = best.get("last_sale_price") or best.get("last_purchase_price")
            qty = float(item.get("quantity") or 0)
            subtotal = round(float(db_price) * qty, 2) if db_price and qty else 0.0
            new_items.append({
                **item,
                "name": db_name,
                "rate_per_unit": float(db_price) if db_price else None,
                "subtotal": subtotal,
                "price_source": "inventory",
                "unit": best.get("unit") or item.get("unit"),
            })
        else:
            # ambiguous — keep as-is, rate stays null, excluded from total
            new_items.append(item)

    # Total: only items with a confirmed DB rate (not null)
    fresh_total = round(sum(
        float(i.get("subtotal") or 0)
        for i in new_items
        if i.get("rate_per_unit") is not None
    ), 2)

    original_total = float(tx.get("total_amount") or 0)
    original_paid = float(tx.get("amount_paid") or 0)

    # If user indicated full payment (paid ≥ AI-calculated total), lock to fresh total
    amount_paid = fresh_total if original_paid >= original_total and original_total > 0 else min(original_paid, fresh_total)
    pending = max(round(fresh_total - amount_paid, 2), 0.0)

    return {
        **tx,
        "items": new_items,
        "total_amount": fresh_total,
        "calculated_total": fresh_total,
        "amount_paid": amount_paid,
        "pending_amount": pending if pending > 0 else None,
        "is_credit": pending > 0,
        "total_matches": True,
    }


# ── Transaction processor ─────────────────────────────────────────────────────

async def _process_tx(
    db: AsyncSession,
    user_id: int,
    tx: dict,
) -> tuple[str | None, TransactionDetail | None, ChatResponse | None]:
    """
    Process a single parsed transaction dict.

    Returns (reply, detail, clarification_response):
    - Customer clarification needed : (None, None, ChatResponse)
    - Processed ok                  : (reply_str, TransactionDetail, None)
    - Error                         : (error_msg, TransactionDetail(error), None)
    """
    tx_type = tx.get("type", "").lower()

    if tx_type not in _VALID_TYPES:
        msg = "Thoda clear likhiye"
        return msg, TransactionDetail(type=tx_type or "unknown", status="error", message=msg), None

    # ── Query ─────────────────────────────────────────────────────────────────
    if tx_type == "query":
        customer_name = tx.get("customer_name")
        if not customer_name:
            msg = "Kis customer ka hisaab chahiye? Naam likhiye"
            return msg, TransactionDetail(type="query", status="error", message=msg), None

        customer = await customer_service.get_or_create(db, user_id, customer_name)
        msg = f"👤 {customer.name} ka baaki: ₹{customer.pending:,.0f}"
        return msg, TransactionDetail(
            type="query",
            status="recorded",
            customer_name=customer.name,
            customer_total_pending=float(customer.pending),
            message=msg,
        ), None

    # ── Amount ────────────────────────────────────────────────────────────────
    try:
        amount = Decimal(str(tx.get("total_amount", 0)))
        if amount < 0:
            raise ValueError
    except (ValueError, Exception):
        msg = "Amount sahi likhiye"
        return msg, TransactionDetail(type=tx_type, status="error", message=msg), None

    raw_items: list = tx.get("items", [])
    note: str | None = tx.get("note")
    is_credit: bool = bool(tx.get("is_credit", False))
    pending_raw = tx.get("pending_amount")
    pending_amount = Decimal(str(pending_raw)) if pending_raw is not None else Decimal("0")
    response_items = _build_items(raw_items)

    # ── Purchase ──────────────────────────────────────────────────────────────
    if tx_type == "purchase":
        await transaction_service.record_purchase(db, user_id, amount, raw_items, note)
        msg = f"🛒 Purchase recorded: ₹{amount:,.0f}"
        return msg, TransactionDetail(
            type="purchase", status="recorded",
            total_amount=float(amount), amount_paid=float(amount),
            is_credit=False, items=response_items, note=note, message=msg,
        ), None

    # ── Expense ───────────────────────────────────────────────────────────────
    if tx_type == "expense":
        await transaction_service.record_expense(db, user_id, amount, note)
        msg = f"💸 Expense added: ₹{amount:,.0f}"
        return msg, TransactionDetail(
            type="expense", status="recorded",
            total_amount=float(amount), amount_paid=float(amount),
            is_credit=False, items=[], note=note, message=msg,
        ), None

    # ── Sale — product name mandatory ─────────────────────────────────────────
    if tx_type == "sale":
        has_product = any(item.get("name", "").strip() for item in raw_items)
        if not has_product:
            q = "Kaunsa product add karna hai?"
            return None, None, ChatResponse(reply=q, clarification_needed=q)

        # Early not-found check: the AI already marked items as price_source: "not_found".
        # Show Add/Skip buttons immediately — before the complete-sale gate — so the user
        # can resolve inventory issues even when customer_name or amount_paid is still null.
        early_not_found = [
            item for item in raw_items
            if item.get("name", "").strip() and item.get("price_source") == "not_found"
        ]
        if early_not_found:
            action_products = [
                InventoryActionProduct(product_name=item["name"])
                for item in early_not_found
            ]
            not_found_names = ", ".join(item["name"] for item in early_not_found)
            msg = (
                f"{not_found_names} not found in inventory. "
                "Add to inventory or skip to continue."
            )
            return None, None, ChatResponse(
                reply=msg,
                inventory_action_needed=action_products,
                pending_transaction=tx,
            )

        # ── Complete sale → Step 3 inventory check + Step 4 confirmation ──────
        if _is_complete_sale(tx):
            # Step 3: Verify every item exists in inventory (confidence ≥ 0.80)
            item_statuses = await _validate_sale_items(db, user_id, raw_items)

            # Bug 3: not-found products → return inline Add/Skip action buttons
            not_found_items = [(item, cat) for item, status, cat in item_statuses if status == "not_found"]
            if not_found_items:
                action_products = [
                    InventoryActionProduct(product_name=item["name"])
                    for item, _ in not_found_items
                ]
                not_found_names = ", ".join(item["name"] for item, _ in not_found_items)
                msg = (
                    f"{not_found_names} not found in inventory. "
                    "Add to inventory or skip to continue."
                )
                return None, None, ChatResponse(
                    reply=msg,
                    inventory_action_needed=action_products,
                    pending_transaction=tx,
                )

            # Bug 4: found items with NULL/zero price in DB → block, ask user to update
            no_price_names: list[str] = []
            for item, status, catalog in item_statuses:
                if status == "found" and catalog.get("matches"):
                    best = catalog["matches"][0]
                    price = best.get("last_sale_price") or best.get("last_purchase_price")
                    if not price or float(price) == 0:
                        no_price_names.append(best["product_name"])
            if no_price_names:
                lines = [
                    f"{name} has no price set in inventory. Please update the price first."
                    for name in no_price_names
                ]
                msg = "\n".join(lines)
                return None, None, ChatResponse(reply=msg, clarification_needed=msg)

            # Bug 4: rebuild all items with DB-confirmed names and prices
            rebuilt_tx = _apply_db_prices(tx, item_statuses)

            # Step 4: Show explicit order summary before any transaction is recorded
            draft = _build_transaction_draft(rebuilt_tx)
            summary = _build_order_summary(rebuilt_tx)
            return None, None, ChatResponse(
                reply=summary,
                transaction_draft=draft,
                pending_transaction=rebuilt_tx,
            )

        # Guard: items collected but amount_paid still unknown → ask before
        # proceeding to customer lookup, which would trigger a premature flow.
        if tx.get("amount_paid") is None:
            q = "Kitna paisa mila? Amount batao"
            return None, None, ChatResponse(reply=q, clarification_needed=q)

    # ── Sale / Payment — need customer ────────────────────────────────────────
    customer_name: str | None = tx.get("customer_name")
    if not customer_name:
        msg = "Customer ka naam likhiye"
        return msg, TransactionDetail(type=tx_type, status="error", message=msg), None

    # MuRIL-enhanced search returns (Customer, score) tuples
    candidates_with_scores = await customer_service.search_by_name_with_muril(
        db, user_id, customer_name
    )
    candidate_objs = [c for c, _ in candidates_with_scores]

    # No match → new customer, ask phone
    if not candidate_objs:
        return None, None, ChatResponse(
            reply=(
                f"'{customer_name}' system mein nahi hain.\n"
                "Unka phone number kya hai? (ya 'skip' likho)"
            ),
            customer_candidates=[],
            pending_transaction=tx,
        )

    # Multiple matches (or single) — always show selection; never auto-confirm
    reply_text = (
        f"'{customer_name}' naam ke {len(candidate_objs)} customer hain — sahi wala select karo "
        if len(candidate_objs) >= 2
        else f"'{customer_name}' naam ka customer mila — confirm karo ya naya customer add karo "
    )
    return None, None, ChatResponse(
        reply=reply_text,
        customer_candidates=_candidate_list(candidates_with_scores),
        pending_transaction=tx,
    )


# ── Bug 3: Inline inventory actions ──────────────────────────────────────────

async def _resume_after_inventory_change(
    db: AsyncSession,
    user_id: int,
    tx: dict,
    prefix_msg: str,
) -> ChatResponse:
    """
    Re-run the full sale validation pipeline on a (possibly mutated) pending_transaction
    and return the next appropriate ChatResponse.  Used by both add and skip paths.
    """
    raw_items = tx.get("items", [])
    if not raw_items:
        return ChatResponse(
            reply=f"{prefix_msg}\nNo remaining products in order.",
            clarification_needed="No remaining products in order.",
        )

    item_statuses = await _validate_sale_items(db, user_id, raw_items)

    # Still some not-found products → show Add/Skip buttons for the remaining ones
    not_found_items = [(item, cat) for item, status, cat in item_statuses if status == "not_found"]
    if not_found_items:
        action_products = [
            InventoryActionProduct(product_name=item["name"])
            for item, _ in not_found_items
        ]
        not_found_names = ", ".join(item["name"] for item, _ in not_found_items)
        msg = f"{prefix_msg}\n{not_found_names} not found in inventory. Add to inventory or skip to continue."
        return ChatResponse(
            reply=msg,
            inventory_action_needed=action_products,
            pending_transaction=tx,
        )

    # Found items with no price → block
    no_price_names: list[str] = []
    for item, status, catalog in item_statuses:
        if status == "found" and catalog.get("matches"):
            best = catalog["matches"][0]
            price = best.get("last_sale_price") or best.get("last_purchase_price")
            if not price or float(price) == 0:
                no_price_names.append(best["product_name"])
    if no_price_names:
        lines = [
            f"{name} has no price set in inventory. Please update the price first."
            for name in no_price_names
        ]
        msg = f"{prefix_msg}\n" + "\n".join(lines)
        return ChatResponse(reply=msg, clarification_needed=msg)

    # All items resolved — rebuild with DB prices and show confirmation
    rebuilt_tx = _apply_db_prices(tx, item_statuses)
    draft = _build_transaction_draft(rebuilt_tx)
    summary = _build_order_summary(rebuilt_tx)
    return ChatResponse(
        reply=f"{prefix_msg}\n\n{summary}",
        transaction_draft=draft,
        pending_transaction=rebuilt_tx,
    )


async def add_to_inventory_and_resume(
    db: AsyncSession,
    user_id: int,
    req: AddToInventoryRequest,
) -> ChatResponse:
    """
    Bug 3 — "Add to Inventory" inline action.
    Saves the product with the user-supplied price, then re-runs the full sale pipeline
    on the pending transaction and returns the next step (more not-found, or confirmation).
    """
    from app.schemas.inventory import InventoryUpsertRequest  # noqa: PLC0415

    upsert = InventoryUpsertRequest(
        product_name=req.product_name,
        quantity=req.quantity,
        unit=req.unit,
        last_sale_price=req.price_per_unit,
    )
    await inventory_service.upsert_inventory(db, user_id, upsert)
    await db.commit()

    prefix = f"{req.product_name} added to inventory at Rs.{req.price_per_unit:.0f}/{req.unit}. Continuing your order..."
    return await _resume_after_inventory_change(db, user_id, req.pending_transaction, prefix)


async def skip_product_and_resume(
    db: AsyncSession,
    user_id: int,
    req: SkipProductRequest,
) -> ChatResponse:
    """
    Bug 3 — "Skip & Continue" inline action.
    Removes the specified products from the pending order with zero trace, then re-runs
    the sale pipeline on the remaining items.
    """
    tx = req.pending_transaction
    raw_items = tx.get("items", [])

    skipped_lower = {n.lower() for n in req.product_names}
    remaining = [
        item for item in raw_items
        if (item.get("name") or "").lower() not in skipped_lower
    ]

    if not remaining:
        skipped_str = ", ".join(req.product_names)
        return ChatResponse(
            reply=f"{skipped_str} skipped. No remaining products in order.",
            clarification_needed="No remaining products in order.",
        )

    updated_tx = {**tx, "items": remaining}
    skipped_str = ", ".join(req.product_names)
    prefix = f"{skipped_str} skipped. Moving to next item..."
    return await _resume_after_inventory_change(db, user_id, updated_tx, prefix)


# ── Main message handler ──────────────────────────────────────────────────────

async def handle_message(
    db: AsyncSession,
    user_id: int,
    raw_message: str,
    # Flutter pre-processor hints (all optional)
    raw_text: str | None = None,
    script: str | None = None,
    lang_hint: str | None = None,
) -> ChatResponse:
    """
    Full pipeline:
      1. Fetch recent history  ┐ (concurrent)
      2. MuRIL analysis        ┘
      3. AI parse (with MuRIL context injected into LLM prompt)
      4. Validate + record transactions
      5. Log → respond (with muril_analysis attached)
    """
    # ── Greeting fast-path ────────────────────────────────────────────────────
    if _GREETING_RE.match(raw_message.strip()):
        reply = _greeting_reply(raw_message)
        await _log(db, user_id, raw_message, None, reply)
        return ChatResponse(reply=reply)

    # ── Step 1 + 2 (concurrent) ───────────────────────────────────────────────
    history_task = asyncio.create_task(_get_recent_logs(db, user_id))
    muril_task = asyncio.create_task(
        muril_service.analyze(raw_message, raw_text=raw_text, lang_hint=lang_hint)
    )
    recent_logs, muril_analysis = await asyncio.gather(history_task, muril_task)

    pending_clarification = _get_pending_clarification(recent_logs)

    client_hints: dict | None = None
    if raw_text or script or lang_hint:
        client_hints = {"raw_text": raw_text, "script": script, "lang_hint": lang_hint}

    muril_response = _format_muril_analysis(muril_analysis)

    # ── Fetch shop_type from user's business ──────────────────────────────────
    business = await db.scalar(select(Business).where(Business.owner_id == user_id))
    shop_type = business.shop_type if business else "general"

    # ── Step 3: AI parse ──────────────────────────────────────────────────────
    parsed = await ai_service.parse_message(
        raw_message,
        history=_build_history(recent_logs),
        pending_clarification=pending_clarification,
        muril_context=muril_analysis,
        client_hints=client_hints,
        shop_type=shop_type,
        db=db,
        user_id=user_id,
    )

    if parsed is None:
        reply = "Samajh nahi aaya, thoda clear likhiye"
        await _log(db, user_id, raw_message, None, reply)
        return ChatResponse(reply=reply, muril_analysis=muril_response)

    clarification = parsed.get("clarification_needed")
    if clarification:
        # Safety net: AI returned "not found in inventory" as clarification with empty transactions.
        # The AI should have included the partial transaction with price_source: "not_found" items.
        # Detect this pattern and generate inventory_action_needed so the UI shows Add/Skip buttons.
        _inv_not_found_re = re.compile(
            r"^(.+?)\s+is not found in inventory", re.IGNORECASE
        )
        if not parsed.get("transactions"):
            not_found_names: list[str] = []
            for line in clarification.strip().split("\n"):
                m = _inv_not_found_re.match(line.strip())
                if m:
                    not_found_names.append(m.group(1).strip())
            if not_found_names:
                action_products = [
                    InventoryActionProduct(product_name=name) for name in not_found_names
                ]
                msg = (
                    f"{', '.join(not_found_names)} not found in inventory. "
                    "Add to inventory or skip to continue."
                )
                minimal_tx: dict = {
                    "type": "sale",
                    "customer_name": None,
                    "items": [
                        {
                            "name": name,
                            "price_source": "not_found",
                            "quantity": None,
                            "unit": None,
                            "rate_per_unit": None,
                            "subtotal": 0,
                        }
                        for name in not_found_names
                    ],
                    "total_amount": 0,
                    "amount_paid": None,
                    "pending_amount": None,
                    "is_credit": False,
                    "calculated_total": 0,
                    "total_matches": True,
                }
                await _log(db, user_id, raw_message, parsed, msg)
                return ChatResponse(
                    reply=msg,
                    inventory_action_needed=action_products,
                    pending_transaction=minimal_tx,
                    muril_analysis=muril_response,
                )

        # Bug 1: scrub any Devanagari that slipped past ai_service post-processing
        clarification = _scrub(clarification)
        await _log(db, user_id, raw_message, parsed, clarification)
        return ChatResponse(
            reply=clarification,
            confidence=parsed.get("confidence", "low"),
            clarification_needed=clarification,
            muril_analysis=muril_response,
        )

    transactions = parsed.get("transactions", [])
    if not transactions:
        reply = "Samajh nahi aaya, thoda clear likhiye"
        await _log(db, user_id, raw_message, parsed, reply)
        return ChatResponse(reply=reply, muril_analysis=muril_response)

    # When resolving a pending clarification, only process the first transaction.
    if pending_clarification and len(transactions) > 1:
        transactions = transactions[:1]

    # ── Step 4: process each transaction ─────────────────────────────────────
    replies: list[str] = []
    tx_details: list[TransactionDetail] = []

    for tx in transactions:
        reply_str, detail, clarification_resp = await _process_tx(db, user_id, tx)

        if clarification_resp is not None:
            log_response = parsed
            if clarification_resp.clarification_needed:
                log_response = {**parsed, "clarification_needed": clarification_resp.clarification_needed}
            await _log(db, user_id, raw_message, log_response, clarification_resp.reply)
            clarification_resp.muril_analysis = muril_response
            return clarification_resp

        if reply_str:
            replies.append(reply_str)
        if detail:
            tx_details.append(detail)

    reply = "\n\n".join(replies) if replies else "Samajh nahi aaya, thoda clear likhiye"
    # Bug 1: final scrub on any reply assembled from AI-generated fields
    reply = _scrub(reply)
    await _log(db, user_id, raw_message, parsed, reply)

    return ChatResponse(
        reply=reply,
        transactions=tx_details,
        confidence=parsed.get("confidence", "high"),
        clarification_needed=parsed.get("clarification_needed"),
        muril_analysis=muril_response,
    )


# ── Confirm-customer ──────────────────────────────────────────────────────────

async def confirm_customer(
    db: AsyncSession,
    user_id: int,
    req: CustomerConfirmRequest,
) -> ChatResponse:
    """Called after user picks a customer from the MuRIL-ranked candidate list."""
    tx = req.pending_transaction

    if req.customer_id is not None:
        customer = await customer_service.get_by_id(db, req.customer_id)
        if customer is None or customer.user_id != user_id:
            return ChatResponse(reply="Customer nahi mila. Phir se try karo")
    else:
        name = (req.customer_name or tx.get("customer_name") or "Unknown").strip()
        if req.customer_phone and req.customer_phone.lower() != "skip":
            existing = await customer_service.get_by_phone(db, user_id, req.customer_phone)
            if existing:
                customer = existing
            else:
                customer = await customer_service.create_customer(
                    db, user_id, name, req.customer_phone
                )
        else:
            customer = await customer_service.get_or_create(db, user_id, name)

    tx_type = tx.get("type", "").lower()
    try:
        amount = Decimal(str(tx.get("total_amount", 0)))
    except Exception:
        return ChatResponse(reply="Amount sahi nahi hai. Phir se try karo")

    raw_items: list = tx.get("items", [])
    note: str | None = tx.get("note")
    is_credit: bool = bool(tx.get("is_credit", False))
    pending_raw = tx.get("pending_amount")
    pending_amount = Decimal(str(pending_raw)) if pending_raw is not None else Decimal("0")
    response_items = _build_items(raw_items)

    if tx_type == "sale":
        await transaction_service.record_sale(
            db, user_id, customer, amount, pending_amount, is_credit, raw_items, note
        )
        msg = _sale_reply(customer.name, amount, pending_amount, is_credit)
        confirmed_log = {"transactions": [], "confidence": "high", "clarification_needed": None}
        phone_part = f" ({customer.phone})" if customer.phone else ""
        await _log(db, user_id, f"[Confirmed: {customer.name}{phone_part}]", confirmed_log, msg)
        return ChatResponse(
            reply=msg,
            transactions=[TransactionDetail(
                type="sale", status="recorded",
                customer_name=customer.name,
                total_amount=float(amount),
                amount_paid=float(amount - pending_amount),
                pending_amount=float(pending_amount) if pending_amount > 0 else None,
                customer_total_pending=float(customer.pending),
                is_credit=is_credit,
                items=response_items,
                note=note,
                message=msg,
            )],
        )

    if tx_type == "payment":
        await transaction_service.record_payment(db, user_id, customer, amount, note)
        msg = (
            f"✅ Payment received\n💰 ₹{amount:,.0f}\n👤 {customer.name}"
            f"\n⏳ Remaining: ₹{customer.pending:,.0f}"
        )
        confirmed_log = {"transactions": [], "confidence": "high", "clarification_needed": None}
        phone_part = f" ({customer.phone})" if customer.phone else ""
        await _log(db, user_id, f"[Confirmed: {customer.name}{phone_part}]", confirmed_log, msg)
        return ChatResponse(
            reply=msg,
            transactions=[TransactionDetail(
                type="payment", status="recorded",
                customer_name=customer.name,
                total_amount=float(amount), amount_paid=float(amount),
                customer_total_pending=float(customer.pending),
                is_credit=False, items=[], note=note, message=msg,
            )],
        )

    return ChatResponse(reply="Transaction type sahi nahi hai")


# ── Confirm-transaction (draft summary card confirmation) ──────────────────────

async def confirm_transaction(
    db: AsyncSession,
    user_id: int,
    req: ConfirmTransactionRequest,
) -> ChatResponse:
    """
    Called when the user taps Confirm on the transaction draft card.
    Applies any edits, searches for the customer, and either:
      - auto-records (single unambiguous match)
      - returns customer_candidates (ambiguous → user selects via /confirm-customer/)
      - asks for phone number (new customer not found)
    """
    tx = req.pending_transaction

    # If customer already selected (e.g. after ambiguity resolution)
    if req.customer_id is not None:
        return await confirm_customer(
            db, user_id,
            CustomerConfirmRequest(customer_id=req.customer_id, pending_transaction=tx),
        )

    customer_name = req.customer_name or tx.get("customer_name")
    if not customer_name:
        return ChatResponse(reply="Customer ka naam batao")

    # MuRIL-enhanced customer search
    candidates_with_scores = await customer_service.search_by_name_with_muril(
        db, user_id, customer_name
    )
    candidate_objs = [c for c, _ in candidates_with_scores]

    # No match → new customer, ask for phone
    if not candidate_objs:
        return ChatResponse(
            reply=(
                f"'{customer_name}' system mein nahi hain.\n"
                "Unka phone number kya hai? (ya 'skip' likho)"
            ),
            customer_candidates=[],
            pending_transaction=tx,
        )

    # Single unambiguous match → auto-confirm and record
    if len(candidate_objs) == 1:
        return await confirm_customer(
            db, user_id,
            CustomerConfirmRequest(customer_id=candidate_objs[0].id, pending_transaction=tx),
        )

    # Multiple matches → let user pick
    reply_text = (
        f"'{customer_name}' naam ke {len(candidate_objs)} customers hain — sahi wala select karo "
    )
    return ChatResponse(
        reply=reply_text,
        customer_candidates=_candidate_list(candidates_with_scores),
        pending_transaction=tx,
    )


# ── Logging ───────────────────────────────────────────────────────────────────

async def _log(
    db: AsyncSession,
    user_id: int,
    user_message: str,
    ai_response: dict | None,
    reply: str,
) -> None:
    db.add(MessageLog(
        user_id=user_id,
        user_message=user_message,
        ai_response=ai_response,
        reply=reply,
    ))
