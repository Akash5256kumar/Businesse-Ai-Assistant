from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class CustomerListItem(BaseModel):
    id: int
    name: str
    phone: str | None = None
    balance: float
    updated_at: datetime
    total_sale: float = 0.0
    total_received: float = 0.0


class CustomerListResponse(BaseModel):
    items: list[CustomerListItem]
    page: int
    has_more: bool
    total_count: int


class CustomerTransactionItem(BaseModel):
    id: int
    type: str
    amount: float
    is_credit: bool = False
    note: str | None = None
    created_at: datetime


class CustomerTransactionListResponse(BaseModel):
    items: list[CustomerTransactionItem]
    page: int
    has_more: bool
