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


# ── Transaction draft (pre-confirmation summary card) ──────────────────────────

class TransactionDraftItem(BaseModel):
    name: str
    quantity: float | None = None
    unit: str | None = None
    rate_per_unit: float | None = None
    subtotal: float = 0.0
    price_source: str = "user"  # "inventory" | "user"


class TransactionDraft(BaseModel):
    type: str
    customer_name: str | None = None
    items: list[TransactionDraftItem] = []
    total_amount: float = 0.0
    amount_paid: float = 0.0
    pending_amount: float = 0.0
    is_credit: bool = False
    note: str | None = None


# ── Bug 3: Inline inventory action (not-found product prompt) ─────────────────

class InventoryActionButton(BaseModel):
    id: str               # "add_to_inventory" | "skip_product"
    label: str
    style: str            # "primary" | "secondary"
    prefill: dict[str, str] | None = None  # {product_name, unit}


class InventoryActionProduct(BaseModel):
    action: str = "PRODUCT_NOT_FOUND"
    product_name: str      # normalized name (for DB lookup and Add to Inventory)
    product_name_raw: str  # original user word (for display)
    message: str           # short user-facing Hinglish notification e.g. "Tamato inventory mein nahi hai."
    buttons: list[InventoryActionButton] = []


# ── Main response ──────────────────────────────────────────────────────────────

class ChatResponse(BaseModel):
    reply: str = ""
    transactions: list[TransactionDetail] = []
    confidence: str = "high"
    clarification_needed: str | None = None
    customer_candidates: list[CustomerCandidate] = []
    pending_transaction: dict | None = None
    transaction_draft: TransactionDraft | None = None
    # MuRIL analysis — null when disabled or on the confirm-customer endpoint.
    muril_analysis: MurilAnalysisResponse | None = None
    # Bug 3: populated when one or more sale products are not found in inventory.
    # Each entry renders "Add to Inventory" + "Skip & Continue" action buttons in the UI.
    inventory_action_needed: list[InventoryActionProduct] = []


# ── Confirm-customer request ───────────────────────────────────────────────────

class CustomerConfirmRequest(BaseModel):
    customer_id: int | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    pending_transaction: dict


# ── Bug 3: Add-to-inventory inline request ────────────────────────────────────

class AddToInventoryRequest(BaseModel):
    product_name: str
    price_per_unit: float
    unit: str
    quantity: float = 0.0
    pending_transaction: dict


# ── Bug 3: Skip-product inline request ────────────────────────────────────────

class SkipProductRequest(BaseModel):
    product_names: list[str]          # one or more products to remove from the order
    pending_transaction: dict


# ── Confirm-transaction request (draft summary card confirmation) ───────────────

class ConfirmTransactionRequest(BaseModel):
    pending_transaction: dict
    customer_id: int | None = None
    customer_name: str | None = None
    customer_phone: str | None = None


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
