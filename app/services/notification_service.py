from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification_log import NotificationLog
from app.schemas.notifications import (
    MarkReadResponse,
    NotificationItem,
    NotificationListResponse,
)

_MAX_ITEMS = 100


async def get_notifications(
    db: AsyncSession, user_id: int
) -> NotificationListResponse:
    result = await db.execute(
        select(NotificationLog)
        .where(NotificationLog.user_id == user_id)
        .order_by(NotificationLog.sent_at.desc())
        .limit(_MAX_ITEMS)
    )
    rows = list(result.scalars().all())

    unread_count = sum(1 for n in rows if not n.is_read)

    return NotificationListResponse(
        items=[
            NotificationItem(
                id=n.id,
                title=n.title,
                body=n.body,
                is_read=n.is_read,
                sent_at=n.sent_at,
                data=n.data,
            )
            for n in rows
        ],
        unread_count=unread_count,
    )


async def mark_all_notifications_read(
    db: AsyncSession, user_id: int
) -> MarkReadResponse:
    result = await db.execute(
        update(NotificationLog)
        .where(
            NotificationLog.user_id == user_id,
            NotificationLog.is_read.is_(False),
        )
        .values(is_read=True)
        .returning(NotificationLog.id)
    )
    marked_ids = list(result.scalars().all())
    await db.commit()
    return MarkReadResponse(
        message="All notifications marked as read.",
        marked_count=len(marked_ids),
    )


async def get_unread_count(db: AsyncSession, user_id: int) -> int:
    result = await db.execute(
        select(func.count()).where(
            NotificationLog.user_id == user_id,
            NotificationLog.is_read.is_(False),
        )
    )
    return result.scalar_one() or 0
