from __future__ import annotations

from pydantic import BaseModel, field_validator


class ChatRequest(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Message cannot be empty")
        return v


class ResponseItem(BaseModel):
    name: str
    quantity: float
    unit: str | None = None
    rate_per_unit: float | None = None
    subtotal: float


class TransactionDetail(BaseModel):
    type: str  # "sale" | "payment" | "purchase" | "expense" | "query"
    status: str = "recorded"  # "recorded" | "error"
    customer_name: str | None = None
    total_amount: float = 0
    amount_paid: float | None = None
    pending_amount: float | None = None
    customer_total_pending: float | None = None
    is_credit: bool = False
    items: list[ResponseItem] = []
    note: str | None = None
    message: str = ""


class CustomerCandidate(BaseModel):
    id: int
    name: str
    phone: str | None = None
    pending: float = 0.0


class ChatResponse(BaseModel):
    reply: str
    transactions: list[TransactionDetail] = []
    confidence: str = "high"
    clarification_needed: str | None = None
    customer_candidates: list[CustomerCandidate] = []
    pending_transaction: dict | None = None


class CustomerConfirmRequest(BaseModel):
    customer_id: int | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    pending_transaction: dict


# ── Internal AI parsing models (not returned to client) ─────────────────────

class ParsedItem(BaseModel):
    name: str
    quantity: float
    unit: str | None = None
    rate_per_unit: float | None = None
    subtotal: float


class ParsedTransaction(BaseModel):
    type: str  # "sale" | "payment" | "purchase" | "expense" | "query"
    customer_name: str | None = None
    total_amount: float = 0
    amount_paid: float | None = None
    pending_amount: float | None = None
    is_credit: bool = False
    items: list[ParsedItem] = []
    calculated_total: float = 0
    total_matches: bool = True
    note: str | None = None


class ParsedAIResponse(BaseModel):
    transactions: list[ParsedTransaction] = []
    confidence: str = "high"
    clarification_needed: str | None = None
