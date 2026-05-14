from __future__ import annotations

from pydantic import BaseModel, field_validator


# ── Inbound request ────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str

    # MuRIL pre-processor hints sent by the Flutter client (all optional).
    # When present, the backend skips duplicate preprocessing and trusts these.
    raw_text: str | None = None      # Original text before client normalisation
    script: str | None = None        # "devanagari" | "latin" | "mixed"
    lang_hint: str | None = None     # BCP-47: "hi-Deva" | "hi-Latn" | "en"

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Message cannot be empty")
        return v


# ── Transaction sub-models ─────────────────────────────────────────────────────

class ResponseItem(BaseModel):
    name: str
    quantity: float
    unit: str | None = None
    rate_per_unit: float | None = None
    subtotal: float


class TransactionDetail(BaseModel):
    type: str  # "sale" | "payment" | "purchase" | "expense" | "query"
    status: str = "recorded"
    customer_name: str | None = None
    total_amount: float = 0
    amount_paid: float | None = None
    pending_amount: float | None = None
    customer_total_pending: float | None = None
    is_credit: bool = False
    items: list[ResponseItem] = []
    note: str | None = None
    message: str = ""


# ── Customer candidate ─────────────────────────────────────────────────────────

class CustomerCandidate(BaseModel):
    id: int
    name: str
    phone: str | None = None
    pending: float = 0.0
    # MuRIL embedding cosine similarity between the user's query and this
    # customer's stored name.  Null when MuRIL is disabled/unavailable.
    similarity_score: float | None = None


# ── MuRIL analysis (returned inside ChatResponse) ─────────────────────────────

class MurilEntityResponse(BaseModel):
    type: str    # PERSON | AMOUNT | PRODUCT | DATE | QUANTITY
    value: str   # surface form extracted from input
    score: float # confidence in [0, 1]


class MurilAnalysisResponse(BaseModel):
    detected_language: str          # BCP-47 tag ("hi-Latn", "hi-Deva", "en")
    intent: str                     # classified intent label
    intent_confidence: float        # in [0, 1]
    entities: list[MurilEntityResponse] = []
    normalized_text: str = ""


# ── Main response ──────────────────────────────────────────────────────────────

class ChatResponse(BaseModel):
    reply: str
    transactions: list[TransactionDetail] = []
    confidence: str = "high"
    clarification_needed: str | None = None
    customer_candidates: list[CustomerCandidate] = []
    pending_transaction: dict | None = None
    # MuRIL analysis — null when disabled or on the confirm-customer endpoint.
    muril_analysis: MurilAnalysisResponse | None = None


# ── Confirm-customer request ───────────────────────────────────────────────────

class CustomerConfirmRequest(BaseModel):
    customer_id: int | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    pending_transaction: dict


# ── Internal AI parsing models (never returned to client) ─────────────────────

class ParsedItem(BaseModel):
    name: str
    quantity: float
    unit: str | None = None
    rate_per_unit: float | None = None
    subtotal: float


class ParsedTransaction(BaseModel):
    type: str
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
