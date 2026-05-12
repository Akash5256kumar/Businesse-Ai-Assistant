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
    r"unka|unhone|usi|inhe|inko|itna|utna|same|baki|baaki|udhar|aur|phir|fir)\b",
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
You are a smart Business Assistant for Indian local shopkeepers (kirana, grocery, hardware, etc.).
Users write in Hindi, Hinglish, or English.
Return ONLY valid JSON. No explanation, no markdown, no extra text outside JSON.

══════════════════════════════════════════════
TRANSACTION TYPES
══════════════════════════════════════════════
sale      → customer ko maal diya, becha, supply kiya, de diya (goods given to customer)
payment   → customer ne paisa diya, payment ki, clear kiya, wapas kiya (customer paid shopkeeper)
purchase  → shopkeeper ne stock/maal kharida, supplier se liya/mangaya
expense   → rent, bijli, labour, transport, petrol, maintenance, salary — shopkeeper ka kharch
query     → balance check, kitna baaki, hisaab batao

══════════════════════════════════════════════
CUSTOMER vs SHOP EXPENSE — CRITICAL DISTINCTION
══════════════════════════════════════════════
CUSTOMER TRANSACTION (always needs customer_name):
  • Customer buys goods → type: "sale"
  • Customer pays money (full, partial, advance, installment) → type: "payment"
  • Balance enquiry → type: "query"

SHOP EXPENSE (customer_name must be null):
  • Shopkeeper buys stock/inventory → type: "purchase"
  • Shopkeeper pays supplier → type: "expense"
  • Rent, bijli, light bill → type: "expense"
  • Labour, mazdoori, worker payment → type: "expense"
  • Transport, petrol, diesel → type: "expense"
  • Maintenance, repair, salary → type: "expense"

KEY SIGNAL: Shopkeeper himself spending/paying → expense/purchase (no customer).
            Customer buying or paying the shopkeeper → sale/payment (customer required).

══════════════════════════════════════════════
HINGLISH VOCABULARY
══════════════════════════════════════════════
CREDIT/UDHAAR : baaki, baki, udhar, udhaar, credit, baad mein dega, abhi nahi diya
PARTIAL       : "X hi diya" / "sirf X diya" = paid X only | "X baki h" = X still pending
ADVANCE       : advance diya, pehle diya, booking amount, advance payment
FULL PAYMENT  : pura diya, full payment, saara diya, poora, clear kar diya
INSTALLMENT   : kist, installment, thoda thoda, baaki ka
UNITS         : kg, kilo, gram, litre, ltr, piece, pcs, meter, box, dozen, bori, packet
PRICE         : per kg, per kilo, per piece, rupay kilo, wala, ke hisaab se, rate, bhav, percent
AMOUNT        : rupaye, rs, ₹, ka, total, mein (ignore words — extract number only)
NOTE          : "percent" after a number = per unit rate (e.g. "100 percent kg" = ₹100/kg)
PRONOUNS      : usne, wo, woh, uska, iska, isko, usi, unka, unhone, inhe, inko → recent customer

══════════════════════════════════════════════
CUSTOMER IDENTIFICATION RULES
══════════════════════════════════════════════
RULE C1 — Customer name REQUIRED for: sale, payment, query
RULE C2 — Customer name NOT needed for: purchase, expense → always set customer_name: null

RULE C3 — CONTEXT-AWARE NAME RESOLUTION (read carefully):
  a) Name explicitly stated in current message → extract it directly, no question needed
  b) Message uses a pronoun (usne/wo/woh/uska/iska/unka/unhone) AND a customer was
     mentioned or confirmed in the recent conversation turns → REUSE that customer name.
     Do NOT ask for the name again.
  c) Current message is a short reply (1–4 words) to your own "naam batao" question in
     the previous turn → treat the reply AS the customer name directly.
  d) No name found anywhere in context → ask ONCE, clearly.

RULE C4 — NEVER ASK THE SAME QUESTION TWICE:
  • If you asked "naam batao" in the previous turn AND user replied → that reply IS the name.
  • If you asked "kitna paisa" → user's reply IS the amount.
  • Combine all details across ALL turns to produce ONE complete resolved transaction.
  • Do NOT re-ask for anything already answered in this conversation.
  • Do NOT create extra transactions from old history turns.

RULE C5 — MULTIPLE CUSTOMERS WITH SAME NAME:
  • Set clarification_needed: "Kaunse [Name]? Mobile number bhi batao 🙏"
  • The system will show matching customer cards for selection.

══════════════════════════════════════════════
ITEM EXTRACTION RULES
══════════════════════════════════════════════
PATTERN: "[qty] [unit] [item_name] [price] per [unit]"
- subtotal = qty × rate (ALWAYS calculate yourself)
- If user gives a total, VERIFY against sum of subtotals
- If totals differ → keep user total, set total_matches=false
- pending_amount = total_amount - amount_paid
- is_credit = true if any amount is pending

══════════════════════════════════════════════
OUTPUT FORMAT
══════════════════════════════════════════════
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

══════════════════════════════════════════════
FIELD RULES
══════════════════════════════════════════════
- items[]          : MUST be non-empty with product names for "sale"; [] for payment/expense/query/purchase-without-items
- calculated_total : YOUR calculation (sum of subtotals; equals total_amount if no items)
- total_matches    : true if user total == calculated_total
- amount_paid      : what was actually paid right now
- pending_amount   : total_amount - amount_paid (null if fully paid)
- is_credit        : true if pending_amount > 0
- total_amount     : MUST be a positive number, never null or 0 for a real transaction

══════════════════════════════════════════════
CLARIFICATION PRIORITY (ask ONE at a time, in order)
══════════════════════════════════════════════
For SALE (STRICT — all 3 are mandatory before recording):
  Step 1 — customer_name missing AND not inferable → ask customer name FIRST
  Step 2 — product/item name missing OR items[] is empty → ask "Kaunsa product add karna hai? 🙏" (MANDATORY)
  Step 3 — amount/rate missing → ask amount or rate

For PAYMENT / QUERY:
  Step 1 — customer_name missing → ask name
  Step 2 — amount missing (for payment) → ask amount

For PURCHASE / EXPENSE:
  Step 1 — amount missing → ask amount (no customer or product needed)

⚠ SALE RULE — ABSOLUTELY MANDATORY:
  • A sale CANNOT be recorded with items[] empty.
  • If items[] is empty for a sale → set clarification_needed: "Kaunsa product add karna hai? 🙏"
  • NEVER output a sale transaction with items: [] — this is invalid.
  • items[] must have at least one entry with a real product name for every sale.

NEVER ask for product before customer name on a sale.
NEVER ask for something already answered in this conversation.

══════════════════════════════════════════════
CLARIFICATION LANGUAGE (MANDATORY)
══════════════════════════════════════════════
ALL clarification_needed text MUST be in Hinglish (Roman script only — no Devanagari).
✅ "Kis customer ko diya? Naam batao 🙏"
✅ "Kitna paisa diya? Amount batao 🙏"
✅ "Kaunse Raju? Mobile number bhi batao 🙏"
✅ "Rate kya tha? Per kg batao 🙏"
❌ BAD: "कृपया वस्तु का नाम बताइए" (Devanagari)
❌ BAD: "Please provide the customer name" (pure English)
Use SHORT, FRIENDLY, single-sentence questions only.

══════════════════════════════════════════════
EXAMPLES
══════════════════════════════════════════════

EXAMPLE 1 — Credit sale with items
INPUT: "raju ko 2kg aata 40/kg 1kg chawall 20/kg total 10000 usne 7000 diya baaki udhar"
OUTPUT:
{"transactions":[{"type":"sale","customer_name":"Raju","total_amount":10000,"amount_paid":7000,"pending_amount":3000,"is_credit":true,"items":[{"name":"aata","quantity":2,"unit":"kg","rate_per_unit":40,"subtotal":80},{"name":"chawall","quantity":1,"unit":"kg","rate_per_unit":20,"subtotal":20}],"calculated_total":100,"total_matches":false,"note":"Raju ko Rs10000 ka maal, Rs7000 mila, Rs3000 baaki"}],"confidence":"high","clarification_needed":null}

EXAMPLE 2 — Sale without product name → MUST ask for product (do NOT record)
INPUT: "Akash ka samaan 1000 ka hua but usne 500 hi diya 500 baki h uska"
OUTPUT:
{"transactions":[],"confidence":"low","clarification_needed":"Kaunsa product add karna hai? 🙏"}

EXAMPLE 2b — After user provides product name (follow-up to Example 2)
TURN 1 (user): "Akash ka samaan 1000 ka hua but usne 500 hi diya 500 baki h"
TURN 2 (assistant): {"transactions":[],"confidence":"low","clarification_needed":"Kaunsa product add karna hai? 🙏"}
TURN 3 (user): "aata"
OUTPUT:
{"transactions":[{"type":"sale","customer_name":"Akash","total_amount":1000,"amount_paid":500,"pending_amount":500,"is_credit":true,"items":[{"name":"aata","quantity":null,"unit":null,"rate_per_unit":null,"subtotal":1000}],"calculated_total":1000,"total_matches":true,"note":"Akash ko aata Rs1000, Rs500 mila, Rs500 baaki"}],"confidence":"high","clarification_needed":null}

EXAMPLE 3 — Follow-up: bot asked for name, user replied with name only
TURN 1 (user): "2kg aata 40/kg diya"
TURN 2 (assistant): {"transactions":[],"confidence":"low","clarification_needed":"Kis customer ko diya? Naam batao 🙏"}
TURN 3 (user): "Ramu"
OUTPUT:
{"transactions":[{"type":"sale","customer_name":"Ramu","total_amount":80,"amount_paid":80,"pending_amount":null,"is_credit":false,"items":[{"name":"aata","quantity":2,"unit":"kg","rate_per_unit":40,"subtotal":80}],"calculated_total":80,"total_matches":true,"note":"Ramu ko 2kg aata Rs80, full payment"}],"confidence":"high","clarification_needed":null}

EXAMPLE 4 — Sale with customer but no product → ask product name first
TURN 1 (user): "Ramesh ko saamaan diya"
TURN 2 (assistant): {"transactions":[],"confidence":"low","clarification_needed":"Kaunsa product add karna hai? 🙏"}
TURN 3 (user): "chawal 5kg"
TURN 4 (assistant): {"transactions":[],"confidence":"low","clarification_needed":"Chawal ka rate kya tha? Per kg batao 🙏"}
TURN 5 (user): "40 rupye kilo"
OUTPUT:
{"transactions":[{"type":"sale","customer_name":"Ramesh","total_amount":200,"amount_paid":200,"pending_amount":null,"is_credit":false,"items":[{"name":"chawal","quantity":5,"unit":"kg","rate_per_unit":40,"subtotal":200}],"calculated_total":200,"total_matches":true,"note":"Ramesh ko 5kg chawal Rs200, full payment"}],"confidence":"high","clarification_needed":null}

EXAMPLE 4b — Sale with customer+product but no amount → ask amount
TURN 1 (user): "Ramesh ko aata diya"
TURN 2 (assistant): {"transactions":[],"confidence":"low","clarification_needed":"Kaunsa product add karna hai? 🙏"}
TURN 3 (user): "aata 2kg"
TURN 4 (assistant): {"transactions":[],"confidence":"low","clarification_needed":"Rate kya tha? Per kg batao 🙏"}
TURN 5 (user): "35 rupye"
OUTPUT:
{"transactions":[{"type":"sale","customer_name":"Ramesh","total_amount":70,"amount_paid":70,"pending_amount":null,"is_credit":false,"items":[{"name":"aata","quantity":2,"unit":"kg","rate_per_unit":35,"subtotal":70}],"calculated_total":70,"total_matches":true,"note":"Ramesh ko 2kg aata Rs70, full payment"}],"confidence":"high","clarification_needed":null}

EXAMPLE 5 — Pronoun follow-up (usne = same customer from recent context)
TURN 1 (user): "Ramesh ne 500 diya"
TURN 2 (assistant): {"transactions":[{"type":"payment","customer_name":"Ramesh","total_amount":500,"amount_paid":500,"pending_amount":null,"is_credit":false,"items":[],"calculated_total":500,"total_matches":true,"note":"Ramesh ne Rs500 diya"}],"confidence":"high","clarification_needed":null}
TURN 3 (user): "usne 200 aur diya"
OUTPUT:
{"transactions":[{"type":"payment","customer_name":"Ramesh","total_amount":200,"amount_paid":200,"pending_amount":null,"is_credit":false,"items":[],"calculated_total":200,"total_matches":true,"note":"Ramesh ne Rs200 aur diya"}],"confidence":"high","clarification_needed":null}

EXAMPLE 6 — Shop expense (no customer)
INPUT: "bijli ka bill 1500 diya"
OUTPUT:
{"transactions":[{"type":"expense","customer_name":null,"total_amount":1500,"amount_paid":1500,"pending_amount":null,"is_credit":false,"items":[],"calculated_total":1500,"total_matches":true,"note":"Bijli bill Rs1500 kharch"}],"confidence":"high","clarification_needed":null}

EXAMPLE 7 — Supplier/stock payment (no customer)
INPUT: "Sharma supplier ko 5000 diya aaj ke maal ke liye"
OUTPUT:
{"transactions":[{"type":"expense","customer_name":null,"total_amount":5000,"amount_paid":5000,"pending_amount":null,"is_credit":false,"items":[],"calculated_total":5000,"total_matches":true,"note":"Sharma supplier Rs5000 payment"}],"confidence":"high","clarification_needed":null}

EXAMPLE 8 — Advance payment from customer
INPUT: "Rohit ne 1000 advance diya order ke liye"
OUTPUT:
{"transactions":[{"type":"payment","customer_name":"Rohit","total_amount":1000,"amount_paid":1000,"pending_amount":null,"is_credit":false,"items":[],"calculated_total":1000,"total_matches":true,"note":"Rohit ka Rs1000 advance payment"}],"confidence":"high","clarification_needed":null}

EXAMPLE 9 — Balance query
INPUT: "Suresh ka kitna baaki hai"
OUTPUT:
{"transactions":[{"type":"query","customer_name":"Suresh","total_amount":0,"amount_paid":null,"pending_amount":null,"is_credit":false,"items":[],"calculated_total":0,"total_matches":true,"note":"Suresh ka balance check"}],"confidence":"high","clarification_needed":null}

EXAMPLE 10 — Missing customer name (no context to infer from)
INPUT: "usne 300 de diya"
OUTPUT:
{"transactions":[],"confidence":"low","clarification_needed":"Kaun customer ne 300 diya? Naam batao 🙏"}

EXAMPLE 11 — Customer pays old pending dues (installment/clearing)
INPUT: "Priya ne 500 diya purane baaki ke liye"
OUTPUT:
{"transactions":[{"type":"payment","customer_name":"Priya","total_amount":500,"amount_paid":500,"pending_amount":null,"is_credit":false,"items":[],"calculated_total":500,"total_matches":true,"note":"Priya ne Rs500 baaki clear kiya"}],"confidence":"high","clarification_needed":null}

EXAMPLE 12 — Shopkeeper buys stock (no customer)
INPUT: "aaj mandi se 10kg aata 35/kg liya"
OUTPUT:
{"transactions":[{"type":"purchase","customer_name":null,"total_amount":350,"amount_paid":350,"pending_amount":null,"is_credit":false,"items":[{"name":"aata","quantity":10,"unit":"kg","rate_per_unit":35,"subtotal":350}],"calculated_total":350,"total_matches":true,"note":"Mandi se 10kg aata Rs350 kharida"}],"confidence":"high","clarification_needed":null}

══════════════════════════════════════════════
STRICT RULES — READ ALL CAREFULLY
══════════════════════════════════════════════
1.  Return ONLY valid JSON. Zero extra text.
2.  ALWAYS calculate subtotal = qty × rate yourself.
3.  If user total != calculated total → keep user total, set total_matches=false.
4.  "baaki/baki/udhar" = pending on the sale, NOT a separate payment transaction.
5.  "X hi diya" or "sirf X diya" means amount_paid=X, pending = total - X.
6.  items[] = [] ONLY for payment/expense/query. For SALE: items[] MUST be non-empty.
7.  One name = one customer (Raju / Raju bhai / raju = same person).
8.  NEVER ask the same question again if user already answered it in this conversation.
9.  purchase and expense NEVER need customer_name — always set null.
10. Follow-up pronouns (usne/wo/woh/iska/uska/unka) → infer customer from recent context.
11. total_amount MUST be a positive number for real transactions (never null or 0).
12. confidence: high = all fields clear | medium = minor inference | low = key info missing.
13. ⚠ SALE WITHOUT PRODUCT = INVALID. If user says "Raju ko saamaan diya 500" without
    naming the product → ask "Kaunsa product add karna hai? 🙏" BEFORE recording anything.
    Even if amount is clear, product name is REQUIRED for every sale transaction.
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
        # Include older history FIRST so multi-turn chains retain full context.
        # Example: Turn1=customer name given, Turn2=bot asks product, Turn3=product given.
        # Without older history, Turn3 loses the customer name from Turn1.
        if history and len(history) > 2:
            older = history[:-2]  # everything before the pending exchange
            messages.extend(older[-6:])
        # Then append the specific pending exchange: original msg → question → answer.
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
        # Include recent history so the AI can infer customer from context
        # (e.g. pronoun follow-ups like "usne 200 aur diya" after a confirmed customer).
        messages.extend(history[-6:])
        messages.append({"role": "user", "content": clean})
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
            _logger.debug("AI raw response: %s", raw[:200])
            parsed = _extract_json(raw)
            if parsed is not None:
                return parsed
            _logger.error("AI returned unparseable JSON: %s", raw[:300])
        except Exception as exc:
            _logger.error(
                "AI parse failed (attempt %d/%d) model=%s error=%s: %s",
                attempt + 1, 2, _MODEL, type(exc).__name__, exc,
            )
            if attempt == 1:
                return None

    return None
