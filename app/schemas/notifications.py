from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class NotificationItem(BaseModel):
    id: int
    title: str
    body: str
    is_read: bool
    sent_at: datetime
    data: dict | None = None


class NotificationListResponse(BaseModel):
    items: list[NotificationItem] = []
    unread_count: int = 0


class MarkReadResponse(BaseModel):
    message: str
    marked_count: int
