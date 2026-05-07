from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI

from app.core.config import settings

_logger = logging.getLogger(__name__)
_client = AsyncOpenAI(api_key=settings.openai_api_key)

_MODEL = "gpt-4o-mini"

# ── Regex helpers ────────────────────────────────────────────────────────────

# If any item unit found → message is complex → skip regex, go to AI
_ITEM_UNITS = re.compile(
    r"\b(kg|kilo|gram|litre|ltr|piece|pcs|meter|dozen|bori|packet|nag|basta|/kg|per\s+kg|per\s+piece)\b",
    re.IGNORECASE,
)
_FOLLOW_UP_HINTS = re.compile(
    r"\b(haan|han|hmm|ok|theek|thik|ye|yeh|wo|woh|usne|uska|iska|isko|"
    r"itna|utna|same|baki|baaki|udhar|aur|phir|fir)\b",
    re.IGNORECASE,
)

# Amount: optional "Rs" prefix then digits (e.g. "Rs 500", "500", "1500.50")
_AMT = r"(?:Rs\s*)?(\d+(?:\.\d+)?)"

# Customer name: 1–4 Hindi/English words (stops before keywords)
_NAME = r"([A-Za-z][A-Za-z\s\.]{1,39}?)"


def _wrap(transactions: list) -> dict:
    return {"transactions": transactions, "confidence": "high", "clarification_needed": None}


def _title(s: str) -> str:
    return " ".join(w.capitalize() for w in s.strip().split())


def _query_tx(name: str) -> dict:
    return {
        "type": "query",
        "customer_name": name,
        "total_amount": 0,
        "amount_paid": None,
        "pending_amount": None,
        "is_credit": False,
        "items": [],
        "calculated_total": 0,
        "total_matches": True,
        "note": f"{name} ka balance check karna hai",
    }


def _payment_tx(name: str, amount: float) -> dict:
    return {
        "type": "payment",
        "customer_name": name,
        "total_amount": amount,
        "amount_paid": amount,
        "pending_amount": None,
        "is_credit": False,
        "items": [],
        "calculated_total": amount,
        "total_matches": True,
        "note": f"{name} ne Rs{amount:.0f} diya",
    }


def _expense_tx(category: str, amount: float) -> dict:
    return {
        "type": "expense",
        "customer_name": None,
        "total_amount": amount,
        "amount_paid": amount,
        "pending_amount": None,
        "is_credit": False,
        "items": [],
        "calculated_total": amount,
        "total_matches": True,
        "note": f"{category.title()} Rs{amount:.0f} ka kharch",
    }


def _try_regex(clean: str) -> dict | None:
    """
    Fast-path parser for simple messages.
    Returns None if message is too complex — AI will handle it.
    """
    # Guard: item units present → AI required
    if _ITEM_UNITS.search(clean):
        return None

    text = clean.lower()

    # ── Query ────────────────────────────────────────────────────────────────
    # "Raju ka kitna baaki hai" / "Raju ka hisaab" / "Raju ka balance"
    m = re.search(
        rf"{_NAME}\s+ka\s+(?:kitna\s+)?(?:baaki|hisaab|balance|udhar)",
        text,
    )
    if m:
        return _wrap([_query_tx(_title(m.group(1)))])

    # "kitna baaki hai Raju"
    m = re.search(rf"kitna\s+(?:baaki|udhar)\s+(?:hai\s+)?{_NAME}", text)
    if m:
        return _wrap([_query_tx(_title(m.group(1)))])

    # ── Payment ──────────────────────────────────────────────────────────────
    # "Raju ne 500 diya/diye/de diya/payment ki"
    # Guard: skip if pending indicator present — partial payment needs AI
    if not re.search(r"\b(baaki|baki|udhar)\b", text):
        m = re.search(
            rf"{_NAME}\s+ne\s+{_AMT}\s*(?:rupaye?|rs)?\s*"
            rf"(?:diya|diye|de\s+diya|payment\s+(?:ki|kiya|diya)|bheja|transfer(?:red)?)",
            text,
        )
        if m:
            name = _title(m.group(1))
            amount = float(m.group(2))
            return _wrap([_payment_tx(name, amount)])

    # ── Expense ──────────────────────────────────────────────────────────────
    # "rent 2000 diya" / "bijli 500" / "transport 300 ka bill"
    m = re.search(
        rf"\b(rent|bijli|transport|labour|labor|petrol|diesel|"
        rf"maintenance|salary|mazdoori|kiraya)\b[^\d]*{_AMT}",
        text,
    )
    if m:
        return _wrap([_expense_tx(m.group(1), float(m.group(2)))])

    return None  # Complex message → send to AI


# ── System prompt ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a precise transaction parser for Indian small businesses.
Users write in Hindi, Hinglish, or English.
Return ONLY valid JSON. No explanation, no markdown.

TRANSACTION TYPES
sale     → maal diya, becha, saamaan diya, supply kiya, de diya
payment  → paisa aaya, usne diya, payment mili, wapas kiya, clear kiya
purchase → kharida, mangaya, stock liya, maal aaya, liya
expense  → bill diya, kharch, rent, bijli, transport, labour
query    → kitna baaki, hisaab batao, balance kya hai

HINGLISH VOCABULARY
PENDING : baaki, baki, udhar, baad mein dega, credit, abhi nahi diya
PARTIAL : "X hi diya" = paid only X | "sirf X diya" = paid only X | "X baki h" = X is still pending
UNITS   : kg, kilo, gram, litre, ltr, piece, pcs, meter, box, dozen, bori, packet
PRICE   : per kg, per kilo, per piece, rupay kilo, wala, ke hisaab se, rate, bhav, percent
AMOUNT  : rupaye, rs, ₹, ka, total, mein (ignore — extract number only)
NOTE    : "percent" after a number means "per unit rate" e.g. "100 percent kg" = ₹100 per kg

ITEM EXTRACTION RULES
PATTERN: "[qty] [unit] [item_name] [price] per [unit]"
- subtotal = qty × rate (ALWAYS calculate yourself)
- If user gives a total, VERIFY against sum of subtotals
- If totals differ → keep user total, set total_matches=false
- pending_amount = total_amount - amount_paid
- is_credit = true if any amount is pending

OUTPUT FORMAT
{
  "transactions": [
    {
      "type": "sale | payment | purchase | expense | query",
      "customer_name": "string or null",
      "total_amount": number,
      "amount_paid": number or null,
      "pending_amount": number or null,
      "is_credit": true or false,
      "items": [
        {
          "name": "item name",
          "quantity": number,
          "unit": "kg | piece | litre | meter | dozen | box | null",
          "rate_per_unit": number or null,
          "subtotal": number
        }
      ],
      "calculated_total": number,
      "total_matches": true or false,
      "note": "short Hinglish summary"
    }
  ],
  "confidence": "high | medium | low",
  "clarification_needed": null or "Hinglish question"
}

FIELD RULES
- items[]          : fill for every sale/purchase with named goods
- calculated_total : YOUR calculation (sum of subtotals)
- total_matches    : true if user total == calculated_total
- amount_paid      : what customer gave right now
- pending_amount   : total_amount - amount_paid (null if fully paid)
- is_credit        : true if pending_amount > 0
- total_amount     : MUST be a positive number, never null or 0 for a real transaction

EXAMPLE
INPUT: "raju ko 2kg aata 40/kg 1kg chawall 20/kg total 10000 usne 7000 diya baaki udhar"
OUTPUT:
{
  "transactions": [
    {
      "type": "sale",
      "customer_name": "Raju",
      "total_amount": 10000,
      "amount_paid": 7000,
      "pending_amount": 3000,
      "is_credit": true,
      "items": [
        {"name":"aata","quantity":2,"unit":"kg","rate_per_unit":40,"subtotal":80},
        {"name":"chawall","quantity":1,"unit":"kg","rate_per_unit":20,"subtotal":20}
      ],
      "calculated_total": 100,
      "total_matches": false,
      "note": "Raju ko Rs10000 ka saamaan diya, Rs7000 mila, Rs3000 baaki"
    }
  ],
  "confidence": "high",
  "clarification_needed": null
}

EXAMPLE 2 — partial payment, no items
INPUT: "Akash ka samaan 1000 ka hua but usne 500 hi diya 500 baki h uska"
OUTPUT:
{
  "transactions": [
    {
      "type": "sale",
      "customer_name": "Akash",
      "total_amount": 1000,
      "amount_paid": 500,
      "pending_amount": 500,
      "is_credit": true,
      "items": [],
      "calculated_total": 1000,
      "total_matches": true,
      "note": "Akash ko Rs1000 ka saamaan diya, Rs500 mila, Rs500 baaki"
    }
  ],
  "confidence": "high",
  "clarification_needed": null
}

EXAMPLE 3 — follow-up: bot asked for name, user replies with just the name
CONVERSATION TURN 1 (user): "2kg aata 40/kg diya"
CONVERSATION TURN 2 (assistant): {"transactions":[],"confidence":"low","clarification_needed":"Kaun customer ke liye sale hai? Naam batayein 🙏"}
CONVERSATION TURN 3 (user): "Ramu"
OUTPUT:
{
  "transactions": [
    {
      "type": "sale",
      "customer_name": "Ramu",
      "total_amount": 80,
      "amount_paid": 80,
      "pending_amount": null,
      "is_credit": false,
      "items": [
        {"name":"aata","quantity":2,"unit":"kg","rate_per_unit":40,"subtotal":80}
      ],
      "calculated_total": 80,
      "total_matches": true,
      "note": "Ramu ko 2kg aata Rs80 ka diya, full payment"
    }
  ],
  "confidence": "high",
  "clarification_needed": null
}

EXAMPLE 4 — follow-up: bot asked for items/rate, user replies with just a total amount
CONVERSATION TURN 1 (user): "Ramesh ko saamaan diya"
CONVERSATION TURN 2 (assistant): {"transactions":[],"confidence":"low","clarification_needed":"Kitna maal diya aur kya rate tha? 🙏"}
CONVERSATION TURN 3 (user): "1500 ka diya"
OUTPUT:
{
  "transactions": [
    {
      "type": "sale",
      "customer_name": "Ramesh",
      "total_amount": 1500,
      "amount_paid": 1500,
      "pending_amount": null,
      "is_credit": false,
      "items": [],
      "calculated_total": 1500,
      "total_matches": true,
      "note": "Ramesh ko Rs1500 ka saamaan diya, full payment"
    }
  ],
  "confidence": "high",
  "clarification_needed": null
}

════════════════════════════════════════
STRICT RULES — READ ALL CAREFULLY
════════════════════════════════════════

1. Return ONLY valid JSON. Zero extra text.
2. ALWAYS calculate subtotal = qty × rate yourself.
3. If user total != calculated total → keep user total, set total_matches=false.
4. "baaki/baki/udhar" = pending amount on the sale, NOT a separate transaction.
5. "X hi diya" or "sirf X diya" means amount_paid=X, pending = total - X.
6. items[] = [] for payment/expense/query types.
7. One name = one customer even if written differently (Raju / Raju bhai / raju = same).

8. ══ CUSTOMER NAME RULE (CRITICAL) ══
   • In a fresh message (no prior context): customer_name MUST be explicitly stated.
   • NEVER guess or hallucinate customer_name from general conversation history.
   • In a FOLLOW-UP (you see your own prior question about the customer name in the
     conversation above): the user's reply IS the customer_name — extract it directly.
   • If customer_name is still absent after the follow-up → customer_name: null,
     set clarification_needed again.

9. ══ FOLLOW-UP RESOLUTION ══
   • You are shown a multi-turn conversation: original message → your clarification
     question → user's answer.
   • If you previously asked for customer_name and the user replied → use that reply
     as customer_name. Do NOT ask for it again.
   • If you previously asked for items/rate and the user replies with ONLY a total amount
     (e.g. "1500 ka diya", "500 rupaye") → accept it as total_amount with items=[].
     Do NOT ask again.
   • Combine details from ALL turns (items/amounts from original + answers from follow-up)
     to produce ONE complete, resolved transaction.
   • Do NOT re-ask for information already provided in this conversation.
   • Do NOT create extra transactions from older history — only resolve the pending one.

10. ══ CLARIFICATION PRIORITY (ask ONE at a time, in order) ══
    For sale / payment:
      Step 1 — If customer_name is null → ask for customer name FIRST.
      Step 2 — If customer confirmed but qty/rate missing → ask for items.
      Step 3 — If amount unclear → ask for amount.
    For purchase / expense:
      Step 1 — amount (no customer needed).
    NEVER ask for items before customer name on a sale/payment.

11. ══ CLARIFICATION LANGUAGE (MANDATORY) ══
    ALL clarification_needed text MUST be in Hinglish (Roman script mix of Hindi+English).
    ✅ GOOD: "Kaun customer ke liye sale hai? Naam batayein 🙏"
    ✅ GOOD: "Rate kya hai? Per kg batayein 🙏"
    ✅ GOOD: "Kitna maal diya aur kya rate tha? 🙏"
    ❌ BAD (pure Devanagari Hindi): "कृपया वस्तु का नाम बताइए"
    ❌ BAD (pure English): "Please provide the customer name"
    Use ONLY Roman script. Never use Devanagari characters.

12. ══ CONFIDENCE ══
    high   → all required fields are present and clear
    medium → inferred something, minor ambiguity
    low    → key info missing → set clarification_needed
"""


# ── Preprocessing ─────────────────────────────────────────────────────────────

def _preprocess(message: str) -> str:
    text = message.strip()
    text = re.sub(r"₹\s*", "Rs ", text)
    text = re.sub(r"\b(rs\.?|inr)\s*", "Rs ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text

    
def _extract_json(raw: str) -> dict | None:
    raw = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return None


def _needs_conversation_context(
    clean: str,
    history: list[dict[str, str]] | None,
    pending_clarification: dict[str, str] | None,
) -> bool:
    if pending_clarification:
        return True
    if not history:
        return False
    if len(clean.split()) <= 4:
        return True
    return bool(_FOLLOW_UP_HINTS.search(clean))


def _build_messages(
    clean: str,
    history: list[dict[str, str]] | None,
    pending_clarification: dict[str, str] | None,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": _SYSTEM_PROMPT}]

    if pending_clarification:
        # Use proper multi-turn format so the LLM sees a natural conversation:
        # original message → bot's clarification question → user's answer.
        # This avoids the "cramped single block" confusion where the model
        # re-asks the same question because it can't distinguish the answer.
        messages.append({"role": "user", "content": pending_clarification["previous_user_message"]})
        messages.append({
            "role": "assistant",
            "content": json.dumps({
                "transactions": [],
                "confidence": "low",
                "clarification_needed": pending_clarification["assistant_question"],
            }),
        })
        messages.append({"role": "user", "content": clean})
    elif history:
        # Include recent history only to detect follow-up amounts/items.
        # customer_name is never resolved from history (enforced by system prompt Rule 8).
        messages.extend(history[-4:])
        messages.append(
            {
                "role": "user",
                "content": (
                    "Use history only to resolve follow-up amounts or items — "
                    "NEVER to infer customer_name.\n"
                    f"Current message: {clean}"
                ),
            }
        )
    else:
        messages.append({"role": "user", "content": clean})

    return messages


# ── Main entry point ──────────────────────────────────────────────────────────

async def parse_message(
    message: str,
    history: list[dict[str, str]] | None = None,
    pending_clarification: dict[str, str] | None = None,
) -> dict | None:
    """
    Hybrid parser:
      1. Regex fast-path  → free, instant (simple queries / payments / expenses)
      2. Groq AI fallback → for complex multi-item messages
    """
    clean = _preprocess(message)
    use_context = _needs_conversation_context(clean, history, pending_clarification)

    # Fast path — no API call needed when the message stands on its own
    quick = None if use_context else _try_regex(clean)
    if quick is not None:
        _logger.debug("regex parsed: %s", quick["transactions"][0]["type"])
        return quick

    # AI path — complex message
    _logger.debug("sending to AI: %s", clean[:60])
    for attempt in range(2):
        try:
            response = await _client.chat.completions.create(
                model=_MODEL,
                messages=_build_messages(clean, history, pending_clarification),
                temperature=0,
                max_tokens=1024,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or ""
            parsed = _extract_json(raw)
            if parsed is not None:
                return parsed
        except Exception as exc:
            _logger.error("AI parse failed (attempt %d): %s", attempt + 1, exc)
            if attempt == 1:
                return None

    return None
