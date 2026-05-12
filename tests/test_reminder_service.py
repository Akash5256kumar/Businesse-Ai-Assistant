from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.models.business import Business
from app.models.customer import Customer
from app.models.reminder_log import ReminderLog
from app.services import reminder_service


def _business(enabled: bool = True) -> Business:
    business = MagicMock(spec=Business)
    business.whatsapp_reminders_enabled = enabled
    business.owner_id = 1
    return business


def _customer(
    *,
    customer_id: int = 7,
    name: str = "Sharma Ji",
    phone: str | None = "+91 9876543210",
    pending: str = "12500",
) -> Customer:
    customer = MagicMock(spec=Customer)
    customer.id = customer_id
    customer.user_id = 1
    customer.name = name
    customer.phone = phone
    customer.pending = Decimal(pending)
    customer.updated_at = datetime(2026, 5, 12, tzinfo=UTC)
    return customer


def _log(customer_id: int, sent_at: datetime) -> ReminderLog:
    log = MagicMock(spec=ReminderLog)
    log.customer_id = customer_id
    log.sent_at = sent_at
    return log


@pytest.mark.asyncio
async def test_get_reminders_overview_marks_latest_whatsapp_status():
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=_business(True))
    db.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(
                scalars=lambda: SimpleNamespace(
                    all=lambda: [
                        _customer(customer_id=1, name="A", phone="+91 9999999999"),
                        _customer(customer_id=2, name="B", phone=None, pending="2500"),
                    ]
                )
            ),
            SimpleNamespace(
                scalars=lambda: SimpleNamespace(
                    all=lambda: [_log(1, datetime(2026, 5, 11, tzinfo=UTC))]
                )
            ),
        ]
    )

    response = await reminder_service.get_reminders_overview(db, 1)

    assert response.whatsapp_auto_enabled is True
    assert [item.status for item in response.items] == ["sent", "pending"]
    assert response.items[0].can_send_whatsapp is True
    assert response.items[1].can_send_whatsapp is False
    assert response.items[1].validation_message is not None


@pytest.mark.asyncio
async def test_update_whatsapp_auto_setting_persists_flag():
    db = AsyncMock()
    business = _business(True)
    db.scalar = AsyncMock(return_value=business)

    response = await reminder_service.update_whatsapp_auto_setting(db, 1, False)

    assert business.whatsapp_reminders_enabled is False
    db.flush.assert_awaited_once()
    assert response.whatsapp_auto_enabled is False


@pytest.mark.asyncio
async def test_send_whatsapp_reminder_rejects_missing_phone():
    db = AsyncMock()
    db.scalar = AsyncMock(side_effect=[_business(True), _customer(phone=None)])

    with pytest.raises(HTTPException) as exc:
        await reminder_service.send_whatsapp_reminder(
            db,
            1,
            7,
            "Reminder text",
        )

    assert exc.value.status_code == 400
    assert "phone number" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_send_whatsapp_reminder_logs_successful_delivery():
    db = AsyncMock()
    customer = _customer()
    db.scalar = AsyncMock(side_effect=[_business(True), customer])
    db.add = MagicMock()

    with patch.object(
        reminder_service,
        "_send_whatsapp_message",
        AsyncMock(
            return_value=reminder_service.WhatsAppDeliveryResult(
                provider="mock",
                provider_message_id="msg-1",
                raw_response={"simulated": True},
            )
        ),
    ):
        response = await reminder_service.send_whatsapp_reminder(
            db,
            1,
            customer.id,
            "Reminder text",
        )

    db.add.assert_called_once()
    db.flush.assert_awaited_once()
    assert response.provider == "mock"
    assert response.reminder.status == "sent"
