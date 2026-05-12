from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.business import Business
from app.models.customer import Customer
from app.models.reminder_log import ReminderLog
from app.schemas.reminders import (
    ReminderItem,
    ReminderListResponse,
    ReminderSettingsResponse,
    SendReminderResponse,
)

_ZERO = Decimal("0")
_WHATSAPP_CHANNEL = "whatsapp"
_PLACEHOLDER_WATI_TOKEN = "your_wati_token"


@dataclass(slots=True)
class WhatsAppDeliveryResult:
    provider: str
    provider_message_id: str | None
    raw_response: dict[str, Any] | None


def build_default_reminder_message(customer_name: str, amount: Decimal | float) -> str:
    rounded_amount = Decimal(str(amount)).quantize(Decimal("1"))
    formatted_amount = f"Rs. {rounded_amount:,.0f}"
    return (
        f"Namaste {customer_name} ji, aapka {formatted_amount} pending hai. "
        "Kripya jaldi payment kar dijiye. Dhanyavaad."
    )


async def get_reminders_overview(
    db: AsyncSession,
    user_id: int,
) -> ReminderListResponse:
    business = await _get_business_for_user(db, user_id)
    customers = await _get_pending_customers(db, user_id)
    reminder_items = await _build_reminder_items(db, user_id, customers)
    return ReminderListResponse(
        items=reminder_items,
        whatsapp_auto_enabled=business.whatsapp_reminders_enabled,
    )


async def update_whatsapp_auto_setting(
    db: AsyncSession,
    user_id: int,
    enabled: bool,
) -> ReminderSettingsResponse:
    business = await _get_business_for_user(db, user_id)
    business.whatsapp_reminders_enabled = enabled
    await db.flush()
    state_text = "enabled" if enabled else "disabled"
    return ReminderSettingsResponse(
        whatsapp_auto_enabled=business.whatsapp_reminders_enabled,
        message=f"WhatsApp auto reminders {state_text} successfully.",
    )


async def send_whatsapp_reminder(
    db: AsyncSession,
    user_id: int,
    customer_id: int,
    message: str,
) -> SendReminderResponse:
    await _get_business_for_user(db, user_id)
    customer = await db.scalar(
        select(Customer).where(
            Customer.user_id == user_id,
            Customer.id == customer_id,
        )
    )
    if customer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Customer not found.",
        )
    if customer.pending <= _ZERO:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This customer has no pending amount to remind.",
        )
    if customer.phone is None or not customer.phone.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Customer phone number is required for WhatsApp reminders.",
        )

    normalized_phone = _normalize_whatsapp_number(customer.phone)
    delivery = await _send_whatsapp_message(normalized_phone, message)

    reminder_log = ReminderLog(
        user_id=user_id,
        customer_id=customer.id,
        channel=_WHATSAPP_CHANNEL,
        status="sent",
        message=message,
        provider=delivery.provider,
        provider_message_id=delivery.provider_message_id,
        provider_response=delivery.raw_response,
    )
    db.add(reminder_log)
    await db.flush()

    reminder = ReminderItem(
        id=customer.id,
        customer_id=customer.id,
        customer_name=customer.name,
        phone=customer.phone,
        amount=float(customer.pending),
        due_date=customer.updated_at,
        status="sent",
        can_send_whatsapp=True,
        validation_message=None,
        last_sent_at=reminder_log.sent_at,
    )
    return SendReminderResponse(
        message="WhatsApp reminder sent successfully.",
        reminder=reminder,
        provider=delivery.provider,
    )


async def _get_business_for_user(db: AsyncSession, user_id: int) -> Business:
    business = await db.scalar(select(Business).where(Business.owner_id == user_id))
    if business is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Business profile not found for this user.",
        )
    return business


async def _get_pending_customers(db: AsyncSession, user_id: int) -> list[Customer]:
    result = await db.execute(
        select(Customer)
        .where(
            Customer.user_id == user_id,
            Customer.pending > _ZERO,
        )
        .order_by(Customer.updated_at.asc(), Customer.pending.desc(), Customer.name.asc())
    )
    return list(result.scalars().all())


async def _build_reminder_items(
    db: AsyncSession,
    user_id: int,
    customers: list[Customer],
) -> list[ReminderItem]:
    if not customers:
        return []

    customer_ids = [customer.id for customer in customers]
    logs_result = await db.execute(
        select(ReminderLog)
        .where(
            ReminderLog.user_id == user_id,
            ReminderLog.channel == _WHATSAPP_CHANNEL,
            ReminderLog.customer_id.in_(customer_ids),
        )
        .order_by(ReminderLog.customer_id.asc(), ReminderLog.sent_at.desc())
    )
    latest_logs: dict[int, ReminderLog] = {}
    for log in logs_result.scalars().all():
        latest_logs.setdefault(log.customer_id, log)

    items: list[ReminderItem] = []
    for customer in customers:
        latest_log = latest_logs.get(customer.id)
        phone = customer.phone.strip() if customer.phone else None
        can_send = bool(phone)
        items.append(
            ReminderItem(
                id=customer.id,
                customer_id=customer.id,
                customer_name=customer.name,
                phone=phone,
                amount=float(customer.pending),
                due_date=customer.updated_at,
                status="sent" if latest_log is not None else "pending",
                can_send_whatsapp=can_send,
                validation_message=None
                if can_send
                else "Add a customer phone number to send this reminder on WhatsApp.",
                last_sent_at=latest_log.sent_at if latest_log is not None else None,
            )
        )
    return items


def _normalize_whatsapp_number(phone: str) -> str:
    digits = "".join(char for char in phone if char.isdigit())
    if len(digits) == 10:
        digits = f"91{digits}"
    if len(digits) < 11:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Customer phone number is not valid for WhatsApp delivery.",
        )
    return digits


def _wati_configured() -> bool:
    endpoint = settings.wati_api_endpoint.strip()
    token = settings.wati_access_token.strip()
    return bool(
        endpoint
        and token
        and token != _PLACEHOLDER_WATI_TOKEN
    )


async def _send_whatsapp_message(
    whatsapp_number: str,
    message: str,
) -> WhatsAppDeliveryResult:
    if not _wati_configured():
        return WhatsAppDeliveryResult(
            provider="mock",
            provider_message_id=None,
            raw_response={"simulated": True},
        )

    url = (
        f"{settings.wati_api_endpoint.rstrip('/')}"
        f"/api/v1/sendSessionMessage/{whatsapp_number}"
    )
    headers = {"Authorization": f"Bearer {settings.wati_access_token.strip()}"}
    params = {"messageText": message}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"WhatsApp provider request failed: {exc}",
        ) from exc

    try:
        payload: dict[str, Any] | None = response.json()
    except ValueError:
        payload = {"body": response.text} if response.text else None

    if response.is_error:
        detail = "WhatsApp provider rejected the reminder message."
        if isinstance(payload, dict):
            detail = (
                payload.get("message")
                or payload.get("error")
                or detail
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail,
        )

    provider_message_id = None
    if isinstance(payload, dict):
        provider_message_id = (
            payload.get("messageId")
            or payload.get("id")
            or payload.get("result")
        )
        if provider_message_id is not None:
            provider_message_id = str(provider_message_id)

    return WhatsAppDeliveryResult(
        provider="wati",
        provider_message_id=provider_message_id,
        raw_response=payload if isinstance(payload, dict) else None,
    )
