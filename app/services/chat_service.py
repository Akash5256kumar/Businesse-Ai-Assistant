from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message_log import MessageLog
from app.schemas.chat import (
    ChatResponse,
    CustomerCandidate,
    CustomerConfirmRequest,
    MurilAnalysisResponse,
    MurilEntityResponse,
    ResponseItem,
    TransactionDetail,
)
from app.services import ai_service, customer_service, transaction_service
from app.services.muril_service import muril_service

_logger = logging.getLogger(__name__)

_VALID_TYPES = {"sale", "payment", "purchase", "expense", "query"}
_HISTORY_LIMIT = 5


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


def _get_pending_clarification(logs: list[MessageLog]) -> dict[str, str] | None:
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
        msg = "Thoda clear likhiye 🙏"
        return msg, TransactionDetail(type=tx_type or "unknown", status="error", message=msg), None

    # ── Query ─────────────────────────────────────────────────────────────────
    if tx_type == "query":
        customer_name = tx.get("customer_name")
        if not customer_name:
            msg = "Kis customer ka hisaab chahiye? Naam likhiye 🙏"
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
        msg = "Amount sahi likhiye 🙏"
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
            q = "Kaunsa product add karna hai? 🙏"
            return None, None, ChatResponse(reply=q, clarification_needed=q)

    # ── Sale / Payment — need customer ────────────────────────────────────────
    customer_name: str | None = tx.get("customer_name")
    if not customer_name:
        msg = "Customer ka naam likhiye 🙏"
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
        f"'{customer_name}' naam ke {len(candidate_objs)} customer hain — sahi wala select karo 👇"
        if len(candidate_objs) >= 2
        else f"'{customer_name}' naam ka customer mila — confirm karo ya naya customer add karo 👇"
    )
    return None, None, ChatResponse(
        reply=reply_text,
        customer_candidates=_candidate_list(candidates_with_scores),
        pending_transaction=tx,
    )


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

    # ── Step 3: AI parse ──────────────────────────────────────────────────────
    parsed = await ai_service.parse_message(
        raw_message,
        history=_build_history(recent_logs),
        pending_clarification=pending_clarification,
        muril_context=muril_analysis,
        client_hints=client_hints,
    )

    if parsed is None:
        reply = "Samajh nahi aaya, thoda clear likhiye 🙏"
        await _log(db, user_id, raw_message, None, reply)
        return ChatResponse(reply=reply, muril_analysis=muril_response)

    clarification = parsed.get("clarification_needed")
    if clarification:
        await _log(db, user_id, raw_message, parsed, clarification)
        return ChatResponse(
            reply=clarification,
            confidence=parsed.get("confidence", "low"),
            clarification_needed=clarification,
            muril_analysis=muril_response,
        )

    transactions = parsed.get("transactions", [])
    if not transactions:
        reply = "Samajh nahi aaya, thoda clear likhiye 🙏"
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

    reply = "\n\n".join(replies) if replies else "Samajh nahi aaya, thoda clear likhiye 🙏"
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
            return ChatResponse(reply="Customer nahi mila. Phir se try karo 🙏")
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
        return ChatResponse(reply="Amount sahi nahi hai. Phir se try karo 🙏")

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

    return ChatResponse(reply="Transaction type sahi nahi hai 🙏")


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
