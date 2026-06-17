from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.notifications import MarkReadResponse, NotificationListResponse
from app.services.notification_service import (
    get_notifications,
    mark_all_notifications_read,
)

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.get("/", response_model=NotificationListResponse)
async def list_notifications(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationListResponse:
    return await get_notifications(db, current_user.id)


@router.patch(
    "/read-all",
    response_model=MarkReadResponse,
    status_code=status.HTTP_200_OK,
)
async def mark_all_read(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MarkReadResponse:
    return await mark_all_notifications_read(db, current_user.id)
