from __future__ import annotations

from datetime import date, datetime
from pydantic import BaseModel


class BusinessInfo(BaseModel):
    name: str
    location: str | None = None


class UserHeader(BaseModel):
    full_name: str | None = None
    user_type: str  # "business" | "customer"
    business: BusinessInfo | None = None  # only for business users
    unread_notifications: int = 0


class DailyStats(BaseModel):
    today_sales: float = 0       # Aaj ki bikri
    today_received: float = 0    # Aaya paisa
    total_pending: float = 0     # Pending (sum across all customers)
    today_expenses: float = 0    # Kharcha


class TopCustomer(BaseModel):
    id: int
    name: str
    pending: float


class RecentTransaction(BaseModel):
    id: int
    type: str                    # "sale" | "payment" | "purchase" | "expense"
    customer_name: str | None = None
    amount: float
    is_credit: bool = False
    date: date


class TransactionListItem(BaseModel):
    id: int
    type: str                    # "sale" | "payment" | "purchase" | "expense"
    customer_name: str | None = None
    amount: float
    is_credit: bool = False
    note: str | None = None
    created_at: date


class TransactionListResponse(BaseModel):
    items: list[TransactionListItem] = []
    page: int
    has_more: bool


class TransactionDetailResponse(BaseModel):
    id: int
    title: str
    subtitle: str
    image_url: str = ""
    description: str
    amount: float
    created_at: datetime
    type: str
    highlights: list[str] = []


class HomeResponse(BaseModel):
    user: UserHeader
    # Fields below are only populated for business users
    stats: DailyStats | None = None
    top_customer: TopCustomer | None = None
    recent_transactions: list[RecentTransaction] = []
