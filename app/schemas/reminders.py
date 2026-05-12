from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ReminderItem(BaseModel):
    id: int
    customer_id: int
    customer_name: str
    phone: str | None = None
    amount: float
    due_date: datetime
    status: Literal["pending", "sent"]
    delivery_channel: Literal["whatsapp"] = "whatsapp"
    can_send_whatsapp: bool
    validation_message: str | None = None
    last_sent_at: datetime | None = None


class ReminderListResponse(BaseModel):
    items: list[ReminderItem]
    whatsapp_auto_enabled: bool
    delivery_channel: Literal["whatsapp"] = "whatsapp"


class ReminderSettingsUpdateRequest(BaseModel):
    whatsapp_auto_enabled: bool


class ReminderSettingsResponse(BaseModel):
    whatsapp_auto_enabled: bool
    delivery_channel: Literal["whatsapp"] = "whatsapp"
    message: str


class SendReminderRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4096)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("message cannot be blank")
        return cleaned


class SendReminderResponse(BaseModel):
    message: str
    reminder: ReminderItem
    provider: str
