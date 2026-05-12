from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.reminders import (
    ReminderListResponse,
    ReminderSettingsResponse,
    ReminderSettingsUpdateRequest,
    SendReminderRequest,
    SendReminderResponse,
)
from app.services.reminder_service import (
    get_reminders_overview,
    send_whatsapp_reminder,
    update_whatsapp_auto_setting,
)

router = APIRouter(prefix="/api/v1/reminders", tags=["reminders"])


@router.get("/", response_model=ReminderListResponse)
async def list_reminders(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReminderListResponse:
    return await get_reminders_overview(db, current_user.id)


@router.patch("/settings", response_model=ReminderSettingsResponse)
async def update_reminder_settings(
    payload: ReminderSettingsUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReminderSettingsResponse:
    return await update_whatsapp_auto_setting(
        db,
        current_user.id,
        payload.whatsapp_auto_enabled,
    )


@router.post(
    "/{customer_id}/send-whatsapp",
    response_model=SendReminderResponse,
    status_code=status.HTTP_200_OK,
)
async def send_reminder(
    customer_id: int,
    payload: SendReminderRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SendReminderResponse:
    return await send_whatsapp_reminder(
        db,
        current_user.id,
        customer_id,
        payload.message,
    )
