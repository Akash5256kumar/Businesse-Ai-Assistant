from __future__ import annotations

from datetime import date
from fastapi import HTTPException, status

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.customer import Customer
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.home import (
    BusinessInfo,
    DailyStats,
    HomeResponse,
    InvoiceItemSchema,
    RecentTransaction,
    TransactionDetailResponse,
    TransactionListItem,
    TransactionListResponse,
    TopCustomer,
    UserHeader,
)


async def get_home_data(db: AsyncSession, user_id: int) -> HomeResponse:
    # ── Load user + business relationship in one query ───────────────────────
    user_result = await db.execute(
        select(User).where(User.id == user_id).options(selectinload(User.business))
    )
    user: User = user_result.scalar_one()

    # ── Build user header ────────────────────────────────────────────────────
    business_info: BusinessInfo | None = None
    if user.user_type == "business" and user.business:
        business_info = BusinessInfo(
            name=user.business.name,
            location=user.business.location,
        )

    user_header = UserHeader(
        full_name=user.full_name,
        user_type=user.user_type,
        business=business_info,
        unread_notifications=0,
    )

    # ── Customer users: return profile-only response ─────────────────────────
    if user.user_type == "customer":
        return HomeResponse(user=user_header)

    # ── Business users: fetch dashboard stats ────────────────────────────────
    today = date.today()

    stats_row = await db.execute(
        select(
            func.coalesce(
                func.sum(Transaction.amount).filter(
                    Transaction.type == "sale",
                    func.date(Transaction.created_at) == today,
                ),
                0,
            ).label("today_sales"),
            func.coalesce(
                func.sum(Transaction.amount).filter(
                    Transaction.type == "payment",
                    func.date(Transaction.created_at) == today,
                ),
                0,
            ).label("today_received"),
            func.coalesce(
                func.sum(Transaction.amount).filter(
                    Transaction.type == "expense",
                    func.date(Transaction.created_at) == today,
                ),
                0,
            ).label("today_expenses"),
        ).where(Transaction.user_id == user_id)
    )
    row = stats_row.one()

    # Total pending across all of this business's customers
    pending_result = await db.execute(
        select(func.coalesce(func.sum(Customer.pending), 0)).where(
            Customer.user_id == user_id
        )
    )
    total_pending = pending_result.scalar_one()

    # Top customer by outstanding balance
    top_result = await db.execute(
        select(Customer)
        .where(Customer.user_id == user_id, Customer.pending > 0)
        .order_by(Customer.pending.desc())
        .limit(1)
    )
    top_customer = top_result.scalar_one_or_none()

    # Recent transactions (with customer name via selectinload)
    recent_result = await db.execute(
        select(Transaction)
        .where(Transaction.user_id == user_id)
        .options(selectinload(Transaction.customer))
        .order_by(Transaction.created_at.desc())
        .limit(10)
    )
    recent_txs = recent_result.scalars().all()

    return HomeResponse(
        user=user_header,
        stats=DailyStats(
            today_sales=float(row.today_sales),
            today_received=float(row.today_received),
            total_pending=float(total_pending),
            today_expenses=float(row.today_expenses),
        ),
        top_customer=(
            TopCustomer(
                id=top_customer.id,
                name=top_customer.name,
                pending=float(top_customer.pending),
            )
            if top_customer
            else None
        ),
        recent_transactions=[
            RecentTransaction(
                id=tx.id,
                type=tx.type,
                customer_name=tx.customer.name if tx.customer else None,
                amount=_effective_amount(tx),
                is_credit=tx.is_credit,
                date=tx.created_at.date(),
            )
            for tx in recent_txs
        ],
    )


async def get_transactions_page(
    db: AsyncSession,
    user_id: int,
    *,
    page: int,
    page_size: int,
) -> TransactionListResponse:
    offset = (page - 1) * page_size

    tx_result = await db.execute(
        select(Transaction)
        .where(Transaction.user_id == user_id)
        .options(selectinload(Transaction.customer))
        .order_by(Transaction.created_at.desc())
        .offset(offset)
        .limit(page_size + 1)
    )
    rows = tx_result.scalars().all()
    has_more = len(rows) > page_size
    transactions = rows[:page_size]

    return TransactionListResponse(
        items=[
            TransactionListItem(
                id=tx.id,
                type=tx.type,
                customer_name=tx.customer.name if tx.customer else None,
                amount=_effective_amount(tx),
                is_credit=tx.is_credit,
                note=tx.note,
                created_at=tx.created_at.date(),
            )
            for tx in transactions
        ],
        page=page,
        has_more=has_more,
    )


def _effective_amount(tx: "Transaction") -> float:
    """Return tx.amount — the agreed total stored at recording time.

    tx.amount is the source of truth for all financial calculations:
    customer.pending is derived from it, so using a different value
    (e.g. items-sum) would create inconsistency between displayed amount
    and pending balance.
    """
    return float(tx.amount)


def _format_amount(value: float) -> str:
    return f"₹{value:,.0f}"


def _item_highlight(item: dict) -> str:
    name = str(item.get("name") or "Item").strip()
    quantity = item.get("quantity")
    unit = str(item.get("unit") or "").strip()
    subtotal = item.get("subtotal")

    quantity_text = ""
    if quantity not in (None, ""):
        quantity_text = f" x {quantity:g}" if isinstance(quantity, (int, float)) else f" x {quantity}"
    unit_text = f" {unit}" if unit else ""
    subtotal_text = ""
    if subtotal not in (None, ""):
        subtotal_text = f" = {_format_amount(float(subtotal))}"
    return f"Item: {name}{quantity_text}{unit_text}{subtotal_text}"


def _detail_title(tx_type: str) -> str:
    return {
        "sale": "Sale",
        "payment": "Payment Received",
        "purchase": "Purchase",
        "expense": "Expense",
    }.get(tx_type, "Transaction")


def _detail_subtitle(tx: Transaction) -> str:
    if tx.customer and tx.customer.name:
        return tx.customer.name
    if tx.note:
        return tx.note
    return f"{_detail_title(tx.type.lower())} entry"


def _detail_description(tx: Transaction) -> str:
    tx_type = tx.type.lower()
    customer_name = tx.customer.name if tx.customer else None
    if tx_type == "sale":
        base = (
            f"A {'credit' if tx.is_credit else 'cash'} sale"
            f"{f' for {customer_name}' if customer_name else ''} was recorded."
        )
    elif tx_type == "payment":
        base = f"A payment{f' from {customer_name}' if customer_name else ''} was recorded."
    elif tx_type == "purchase":
        base = "A purchase entry for stock or supplies was recorded."
    elif tx_type == "expense":
        base = "A business expense was recorded."
    else:
        base = "A transaction entry was recorded."
    if tx.note:
        return f"{base} Note: {tx.note}"
    return base


def _detail_highlights(tx: Transaction) -> list[str]:
    highlights: list[str] = [f"Type: {_detail_title(tx.type.lower())}"]
    if tx.customer and tx.customer.name:
        highlights.append(f"Customer: {tx.customer.name}")
    if tx.type.lower() == "sale":
        highlights.append(f"Mode: {'Credit sale' if tx.is_credit else 'Cash sale'}")
    highlights.append(f"Total Amount: {_format_amount(float(tx.amount))}")
    if tx.type.lower() == "sale" and tx.is_credit:
        amount_paid = float(tx.amount) - float(tx.pending_amount or 0)
        if amount_paid > 0:
            highlights.append(f"Paid: {_format_amount(amount_paid)}")
    if tx.customer and float(tx.customer.pending) > 0:
        highlights.append(
            f"Customer Pending (current): {_format_amount(float(tx.customer.pending))}"
        )
    if tx.note:
        highlights.append(f"Note: {tx.note}")
    for item in tx.items or []:
        if isinstance(item, dict):
            highlights.append(_item_highlight(item))
    return highlights


def _build_invoice_items(tx: Transaction) -> list[InvoiceItemSchema]:
    result: list[InvoiceItemSchema] = []
    for item in tx.items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        raw_qty = item.get("quantity")
        quantity = float(raw_qty) if raw_qty not in (None, "") else 1.0
        raw_sub = item.get("subtotal")
        subtotal = float(raw_sub) if raw_sub not in (None, "") else 0.0
        # Prefer stored rate_per_unit; fall back to subtotal/quantity
        raw_rate = item.get("rate_per_unit")
        if raw_rate not in (None, ""):
            rate = float(raw_rate)
        else:
            rate = subtotal / quantity if quantity else subtotal
        result.append(InvoiceItemSchema(name=name, quantity=quantity, rate=rate, subtotal=subtotal))
    return result


async def get_transaction_detail(
    db: AsyncSession,
    user_id: int,
    transaction_id: int,
) -> TransactionDetailResponse:
    result = await db.execute(
        select(Transaction)
        .where(Transaction.id == transaction_id, Transaction.user_id == user_id)
        .options(selectinload(Transaction.customer))
    )
    tx = result.scalar_one_or_none()
    if tx is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found",
        )

    current_customer_pending = float(tx.customer.pending) if tx.customer else 0.0
    tx_total = float(tx.amount)
    # Transaction-level pending: what was NOT paid at recording time.
    tx_pending = float(tx.pending_amount) if tx.pending_amount is not None else (tx_total if tx.is_credit else 0.0)
    tx_paid = max(tx_total - tx_pending, 0.0)

    return TransactionDetailResponse(
        id=tx.id,
        title=_detail_title(tx.type.lower()),
        subtitle=_detail_subtitle(tx),
        image_url="",
        description=_detail_description(tx),
        amount=tx_total,
        amount_paid=tx_paid,
        pending_amount=tx_pending,
        customer_pending=current_customer_pending,
        is_credit=tx.is_credit,
        customer_name=tx.customer.name if tx.customer else None,
        customer_phone=tx.customer.phone if tx.customer else None,
        items=_build_invoice_items(tx),
        created_at=tx.created_at,
        type=tx.type.lower(),
        highlights=_detail_highlights(tx),
    )
