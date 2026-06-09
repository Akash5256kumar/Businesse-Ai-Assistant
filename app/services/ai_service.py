from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.shop_context import get_shop_context
from app.services.ai_tools import TOOLS, execute_tool

_logger = logging.getLogger(__name__)
_client = AsyncOpenAI(api_key=settings.openai_api_key)

_MODEL = "gpt-4o-mini"

# ── Bug 1: Devanagari post-processing ────────────────────────────────────────

_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]+")


def _has_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI_RE.search(text))


def _any_devanagari(data: Any) -> bool:
    """Recursively check all string values in a JSON-serializable structure."""
    if isinstance(data, str):
        return _has_devanagari(data)
    if isinstance(data, dict):
        return any(_any_devanagari(v) for v in data.values())
    if isinstance(data, list):
        return any(_any_devanagari(item) for item in data)
    return False


def _strip_devanagari(text: str) -> str:
    """Remove Devanagari characters from a string (last-resort fallback)."""
    return _DEVANAGARI_RE.sub("", text).strip()


def _strip_devanagari_from_parsed(data: Any) -> Any:
    """Walk a parsed JSON structure and remove Devanagari from all string fields."""
    if isinstance(data, str):
        return _strip_devanagari(data)
    if isinstance(data, dict):
        return {k: _strip_devanagari_from_parsed(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_strip_devanagari_from_parsed(item) for item in data]
    return data


# ── Bug 2 Step 1: Dedicated product name extraction ───────────────────────────

_PRODUCT_EXTRACT_SYSTEM = """\
Extract product items from a Hindi/Hinglish shop order.
Return ONLY valid JSON — no extra text.

{"items": [{"product": "<clean_name>", "quantity": <number_or_null>, "unit": "<unit_or_null>"}]}

Rules for product:
  - Lowercase only
  - Strip ALL filler words: bhai, de do, wala, please, yaar, dena, lena, dijiye, chahiye, hai, etc.
  - Strip customer names, pronouns, verbs, instructions
  - Keep ONLY the product commodity name (e.g. atta, chawal, daal, paneer)

Rules for quantity:
  - Extract the numeric value (e.g. "5 kilo" → 5, "2 dozen" → 2)
  - null if no quantity mentioned

Rules for unit:
  - Normalize: kilo/kilogram → kg | gram/grams → g | litre/liter/ltr → litre
  - piece/pcs/pc → piece | dozen → dozen | packet/pack → packet
  - null if no unit mentioned

Examples:
"bhai 5 kilo wala atta de do" → {"items":[{"product":"atta","quantity":5,"unit":"kg"}]}
"Raju ko 2kg chawal 1kg daal diya" → {"items":[{"product":"chawal","quantity":2,"unit":"kg"},{"product":"daal","quantity":1,"unit":"kg"}]}
"paneer dena" → {"items":[{"product":"paneer","quantity":null,"unit":null}]}
"rice, sugar, aata leke gaya — 5kg each" → {"items":[{"product":"rice","quantity":5,"unit":"kg"},{"product":"sugar","quantity":5,"unit":"kg"},{"product":"aata","quantity":5,"unit":"kg"}]}
"""


async def extract_products_from_text(raw_message: str) -> list[dict]:
    """
    Step 1: Dedicated product-name extraction from raw transcription.
    Separates product name from quantity, unit, and filler words.
    Returns list of {product, quantity, unit} with clean, normalised product names.
    """
    try:
        resp = await _client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _PRODUCT_EXTRACT_SYSTEM},
                {"role": "user", "content": raw_message},
            ],
            temperature=0,
            max_tokens=256,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        items = data.get("items", [])
        for item in items:
            if isinstance(item.get("product"), str):
                item["product"] = item["product"].strip().lower()
        return items
    except Exception as exc:
        _logger.warning("Product extraction step failed: %s", exc)
        return []


def _build_product_context_section(catalog_results: list[dict]) -> str:
    """
    Build a system-prompt section from Step 1 + Step 2 pre-analysis results.
    Gives the AI strong, grounded hints about which products were found and at what confidence.
    """
    if not catalog_results:
        return ""

    lines: list[str] = [
        "\n\n══════════════════════════════════════════════",
        "PRODUCT PRE-ANALYSIS — Step 1 (extraction) + Step 2 (catalog match)",
        "Use this as authoritative context. Call get_recent_price only if rate is missing below.",
        "══════════════════════════════════════════════",
    ]

    for res in catalog_results:
        extracted = res.get("extracted", {})
        matches = res.get("catalog_matches", {})
        product = extracted.get("product", "")
        qty = extracted.get("quantity")
        unit = extracted.get("unit")
        top_conf = matches.get("top_match_confidence", 0.0)
        match_list = matches.get("matches", [])

        qty_str = f" qty={qty}" if qty is not None else ""
        unit_str = f" unit={unit}" if unit else ""
        lines.append(f"  '{product}'{qty_str}{unit_str}:")

        if top_conf >= 0.80 and match_list:
            best = match_list[0]
            price = best.get("last_sale_price") or best.get("last_purchase_price")
            price_str = f"Rs{price}" if price else "no price stored"
            lines.append(
                f"    → FOUND in catalog: '{best['product_name']}' "
                f"(confidence={top_conf:.0%}, {price_str}/{best.get('unit','?')})"
            )
        elif top_conf >= 0.50 and match_list:
            names = [m["product_name"] for m in match_list[:3]]
            lines.append(
                f"    → AMBIGUOUS ({top_conf:.0%}): {', '.join(names)} "
                "— set rate_per_unit: null, price_source: 'ambiguous'; user picks from dropdown"
            )
        else:
            lines.append(
                f"    → ⛔ NOT FOUND in catalog ({top_conf:.0%}) "
                "— set rate_per_unit: null, price_source: 'not_found'; "
                "ALWAYS include in transactions[]. Set clarification_needed: null. "
                "The BACKEND shows Add/Skip buttons. DO NOT set clarification_needed to a 'not found' message. "
                "DO NOT ask user for price. DO NOT suggest any manual entry or workaround."
            )

    lines.append("")
    return "\n".join(lines)

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

_MURIL_INTENT_TO_TX_TYPE: dict[str, str] = {
    "ADD_SALE": "sale",
    "ADD_PAYMENT": "payment",
    "VIEW_BALANCE": "query",
    "ADD_EXPENSE": "expense",
    "VIEW_TRANSACTIONS": "query",
    "ADD_CUSTOMER": "sale",   # Closest billing type
    "SEND_REMINDER": "query",
}

_SYSTEM_PROMPT = """\
You are a smart Business Assistant for Indian local shopkeepers (kirana, grocery, hardware, etc.).
Users write in Hindi, Hinglish, or English.
Return ONLY valid JSON. No explanation, no markdown, no extra text outside JSON.

══════════════════════════════════════════════
TRANSACTION TYPES
══════════════════════════════════════════════
sale      → customer ko maal diya, becha, supply kiya, de diya (goods givens to customer)
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

RULE C4 — NEVER ASK THE SAME QUESTION TWICE — ACCUMULATE STATE:
  • If you asked "naam batao" in the previous turn AND user replied → that reply IS the name.
  • If you asked "kitna paisa" → user's reply IS the amount.
  • Combine all details across ALL turns to produce ONE complete resolved transaction.
  • Do NOT re-ask for anything already answered in this conversation.
  • Do NOT create extra transactions from old history turns.
  • CRITICAL: Your own previous assistant messages in history contain partial transaction state
    (customer_name, items[], amounts already resolved). READ them and CARRY THEM FORWARD.
    If history shows transactions[0].customer_name = "Rakesh", do NOT ask for the name again.
    If history shows items already collected, merge new items IN — don't replace them.

RULE C5 — MULTIPLE CUSTOMERS WITH SAME NAME:
  • Set clarification_needed: "Kaunse [Name]? Mobile number bhi batao 🙏"
  • The system will show matching customer cards for selection.

══════════════════════════════════════════════
AUTO PRICE FETCHING — CRITICAL
══════════════════════════════════════════════
⚠ ABSOLUTE RULE: NEVER ASK THE USER FOR PRICE OR RATE OF ANY PRODUCT.
All product prices MUST come from the database. No exceptions. No workarounds.

⛔ HARD INVENTORY RULE — read carefully:
  A sale can ONLY proceed when EVERY product is confirmed to exist in the inventory database.
  • If a product is NOT found → you MUST NOT collect further info for that product.
  • Do NOT ask the user for price, rate, or suggest any manual workaround.
  • Do NOT proceed with rate_per_unit: null for a not-found product.
  • The BACKEND will detect not_found items and show "Add to Inventory" / "Skip" action buttons.
  This rule cannot be overridden by any user request or conversation context.

For EVERY sale with a product name identified:
  1. ALWAYS call get_recent_price tool for EVERY product whose rate_per_unit is not yet known.
     Call ALL missing-rate products in the SAME turn (parallel tool calls) — do NOT ask the user first.
  2. If tool returns found=true → use that rate directly. Set "price_source":"inventory".
     IMPORTANT: Keep the product name exactly as the USER said it — do NOT replace with the DB product_name.
     NEVER ask the user for price under any circumstances.
  3. If tool returns found=false AND ambiguous=true →
     Keep the product name EXACTLY as the USER said it. Set rate_per_unit: null, price_source: "ambiguous".
     Do NOT ask in chat which product they meant — the user will pick from the product dropdown.
     NEVER output "Kaunsa [product]? Options: ..." for ambiguous products.
  4. ⛔ If tool returns found=false (not ambiguous) →
     Product does NOT exist in the inventory database.
     Set rate_per_unit: null, price_source: "not_found".
     ⛔ CRITICAL — ALWAYS include the item in transactions[] with price_source: "not_found".
     ⛔ CRITICAL — Set clarification_needed: null. DO NOT write the "not found in inventory" message yourself.
     The BACKEND automatically shows "Add to Inventory" and "Skip & Continue" buttons to the user.
     NEVER return transactions: [] for a not-found product — the transaction MUST be in transactions[].
     STRICTLY FORBIDDEN: asking for price or any product field, setting clarification_needed to any "not found" message.

SPECIAL CASE — user says "mujhe nhi pata", "db se fetch karo", "check karo inventory",
"I don't remember the rate", "app se dekh lo", etc.:
  → Call get_recent_price for EVERY product still missing a rate.
  → If found=true → use immediately.
  → If found=false AND ambiguous → keep rate_per_unit: null, price_source: "ambiguous".
  → If found=false (not ambiguous) → set price_source: "not_found". Include in transactions[]. clarification_needed: null.
  → NEVER ask user for rate or suggest workarounds. NEVER set clarification_needed to a "not found" message.

Never skip the tool call when a product name is identified and rate is unknown.

══════════════════════════════════════════════
PAYMENT AMOUNT — MANDATORY FOR EVERY SALE
══════════════════════════════════════════════
  • amount_paid is REQUIRED for every sale transaction.
  • If user has NOT explicitly stated how much was paid → ALWAYS ask: "Kitna paisa mila? Amount batao 🙏"
  • Do NOT assume full payment (amount_paid = total) unless user explicitly says:
    "poora diya", "full payment", "saara diya", "sab diya", "cash diya", "online diya", "paid in full"
  • Only after knowing amount_paid → set is_credit=true if pending_amount > 0.
  • pending_amount = total_amount - amount_paid (null if fully paid)

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
          "price_source": "inventory | ambiguous | not_found | user",
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
PARTIAL STATE ACCUMULATION — CRITICAL
══════════════════════════════════════════════
When clarification_needed is NOT null AND you already have some transaction info:
  • ALWAYS include the partial transaction in transactions[] with fields known so far.
  • Set null (not omit) for fields you still need to ask.
  • This gives the next turn structured memory — without it, you will forget customer names,
    product names, amounts, etc. across turns and re-ask questions already answered.

ONLY use transactions: [] when you have ZERO transaction info (e.g. bare "Sale entry" message).

EXAMPLES of partial state:
  After user gives customer name (still need product):
    transactions: [{"type":"sale","customer_name":"Rakesh","items":[],"total_amount":null,"amount_paid":null,...}]
  After user gives product names but no quantities or rates:
    transactions: [{"type":"sale","customer_name":"Rakesh","items":[{"name":"Rice","quantity":null,"unit":null,"rate_per_unit":null,"subtotal":0},{"name":"Daal","quantity":null,"unit":null,"rate_per_unit":null,"subtotal":0}],"total_amount":null,"amount_paid":null,...}]
  After DB fetch found rates but quantities still missing:
    transactions: [{"type":"sale","customer_name":"Rakesh","items":[{"name":"Rice","quantity":null,"unit":"kg","rate_per_unit":50,"price_source":"inventory","subtotal":0},{"name":"Daal","quantity":null,"unit":"kg","rate_per_unit":80,"price_source":"inventory","subtotal":0}],"total_amount":null,"amount_paid":null,...}]
  After quantities given (still need amount_paid):
    transactions: [{"type":"sale","customer_name":"Rakesh","items":[{"name":"Rice","quantity":2,"unit":"kg","rate_per_unit":50,"subtotal":100},{"name":"Daal","quantity":5,"unit":"kg","rate_per_unit":80,"subtotal":400}],"total_amount":500,"amount_paid":null,...}]
  After user gives rate but not amount_paid:
    transactions: [{"type":"sale","customer_name":"Rakesh","items":[{"name":"Rice","quantity":2,"unit":"kg","rate_per_unit":50,"subtotal":100}],"total_amount":100,"amount_paid":null,...}]

══════════════════════════════════════════════
FIELD RULES
══════════════════════════════════════════════
- items[]          : MUST be non-empty with product names for "sale"; [] for payment/expense/query/purchase-without-items
- items[].quantity : MANDATORY for every sale item — MUST be a positive number. Set null only while waiting for user to provide it; a sale with any null quantity CANNOT be confirmed.
- items[].unit     : kg/litre/piece/dozen/packet/box/meter/null
- items[].rate_per_unit : price per unit — MUST come from DB via get_recent_price.
                          found=true  → use returned rate, price_source: "inventory"
                          found=false, ambiguous=true  → null, price_source: "ambiguous"
                          found=false (not ambiguous)  → null, price_source: "not_found"
                          ⛔ For not_found: include in transactions[], clarification_needed: null.
                          NEVER ask user for rate or any product field for not-found products.
- items[].subtotal : ALWAYS calculate = quantity × rate_per_unit (0 if either is null)
- calculated_total : YOUR calculation (sum of subtotals; equals total_amount if no items)
- total_matches    : true if user total == calculated_total
- amount_paid      : what was actually paid right now
- pending_amount   : total_amount - amount_paid (null if fully paid)
- is_credit        : true if pending_amount > 0
- total_amount     : MUST be a positive number, never null or 0 for a real transaction

══════════════════════════════════════════════
CLARIFICATION PRIORITY (ask in order — combine related gaps into one question)
══════════════════════════════════════════════

⛔ OVERRIDE — NOT-FOUND PRODUCTS ARE ABSOLUTE HIGHEST PRIORITY:
  The moment get_recent_price returns found=false (not ambiguous) for ANY item:
  → Set clarification_needed: null — REGARDLESS of any other missing field.
  → DO NOT ask for customer name, quantity, amount, or anything else.
  → Include ALL items in transactions[] with the not_found item(s) marked.
  → The BACKEND shows "Add to Inventory" / "Skip" buttons IMMEDIATELY.
  → The backend collects missing fields AFTER the user resolves inventory.
  This override fires before Steps 1–5 below. Nothing else takes priority.

For SALE (STRICT — ALL steps are mandatory before recording):
  Step 1 — customer_name missing AND not inferable → ask customer name FIRST
             ⛔ SKIP if any item has price_source "not_found" — see OVERRIDE above.
  Step 2 — product/item name missing OR items[] is empty → ask "Kaunsa product add karna hai?" (MANDATORY)
  Step 3 — quantity missing for ANY found item → MUST ask quantity BEFORE asking for amount.
             ⚠ THIS IS MANDATORY — you CANNOT skip to Step 5 if any item has quantity: null.
             Example: "Kitna diya? Rice ki quantity batao (kg/piece/litre)"
             Example (multi-item): "Kitna diya? Rice, daal ki quantity batao"
             Note: NEVER ask for rate — DB fetches it automatically via get_recent_price.
             ⛔ SKIP if any item has price_source "not_found" — see OVERRIDE above.
  Step 4 — rate_per_unit missing for ANY item → call get_recent_price tool FIRST.
             If found=true → use returned rate, price_source: "inventory".
             If found=false AND ambiguous → price_source: "ambiguous", rate_per_unit: null.
             If found=false (not ambiguous) → price_source: "not_found", rate_per_unit: null.
             ⛔ For not_found: clarification_needed MUST be null. ALWAYS include in transactions[].
             The BACKEND detects not_found items and shows Add/Skip buttons IMMEDIATELY.
             NEVER ask ANY question when a not_found item exists.
  Step 5 — amount_paid missing → ask "Kitna paisa mila? Amount batao"
             ⛔ SKIP if any item has price_source "not_found" — see OVERRIDE above.
             ⛔ SKIP if any item still has quantity: null (Step 3 must fire first).

QUANTITY RULES — MANDATORY:
  • quantity MUST be a positive number for every item in a sale.
  • NEVER record a sale item with quantity: null or quantity: 0.
  • If user gives product names but no quantities → set quantity: null and ask IMMEDIATELY in this turn.
  • Do NOT ask for amount_paid if any item still has quantity: null — ask quantity first.
  • quantity and rate CAN be asked together in one message: "Rice ki quantity aur rate batao"
  • Once quantity is known, calculate subtotal = quantity × rate_per_unit.
  • ⛔ EXCEPTION: If any item has price_source "not_found", do NOT ask for quantity.
    Set quantity: null for those items. The user resolves inventory first, then provides quantity.

For PAYMENT / QUERY:
  Step 1 — customer_name missing → ask name
  Step 2 — amount missing (for payment) → ask amount

For PURCHASE / EXPENSE:
  Step 1 — amount missing → ask amount (no customer or product needed)

⚠ SALE RULE — ABSOLUTELY MANDATORY:
  • A sale CANNOT be recorded with items[] empty.
  • If items[] is empty for a sale → set clarification_needed: "Kaunsa product add karna hai?"
  • NEVER output a sale transaction with items: [] — this is invalid.
  • items[] must have at least one entry with a real product name for every sale.
  • Every item MUST have quantity > 0 before the transaction can be confirmed.

NEVER ask for product before customer name on a sale.
NEVER ask for something already answered in this conversation.

══════════════════════════════════════════════
LANGUAGE RULE — MANDATORY FOR ALL TEXT
══════════════════════════════════════════════
ALL text in every field (clarification_needed, note, reply hints) MUST be in Roman Hinglish.
NEVER use Devanagari characters anywhere — even if the user writes in Devanagari, always reply in Roman script.
✅ "Kis customer ko diya? Naam batao"
✅ "Kitna paisa diya? Amount batao"
✅ "Kaunse Raju? Mobile number bhi batao"
❌ BAD: "Rate kya tha? Per kg batao" (asks for rate — forbidden)
❌ BAD: "कृपया वस्तु का नाम बताइए" (Devanagari — forbidden)
❌ BAD: "Please provide the customer name" (pure English — forbidden)
Use SHORT, FRIENDLY, single-sentence questions only.

EMOJI USAGE (MANDATORY):
Only use these specific emojis — nothing else: ✅ ❌ 💰 👤 ⏳ 💸 🛒
Do NOT use 🙏 or any other emoji not listed above.
In clarification_needed strings: use NO emojis — plain Hinglish text only.

══════════════════════════════════════════════
EXAMPLES
══════════════════════════════════════════════

EXAMPLE 1 — Credit sale with items
INPUT: "raju ko 2kg aata 40/kg 1kg chawall 20/kg total 10000 usne 7000 diya baaki udhar"
OUTPUT:
{"transactions":[{"type":"sale","customer_name":"Raju","total_amount":10000,"amount_paid":7000,"pending_amount":3000,"is_credit":true,"items":[{"name":"aata","quantity":2,"unit":"kg","rate_per_unit":40,"subtotal":80},{"name":"chawall","quantity":1,"unit":"kg","rate_per_unit":20,"subtotal":20}],"calculated_total":100,"total_matches":false,"note":"Raju ko Rs10000 ka maal, Rs7000 mila, Rs3000 baaki"}],"confidence":"high","clarification_needed":null}

EXAMPLE 2 — Sale without product name → ask product, include partial state with customer+amount
INPUT: "Akash ka samaan 1000 ka hua but usne 500 hi diya 500 baki h uska"
OUTPUT:
{"transactions":[{"type":"sale","customer_name":"Akash","total_amount":1000,"amount_paid":500,"pending_amount":500,"is_credit":true,"items":[],"calculated_total":1000,"total_matches":true,"note":null}],"confidence":"low","clarification_needed":"Kaunsa product add karna hai? 🙏"}

EXAMPLE 2b — After user provides product name (follow-up to Example 2)
TURN 1 (user): "Akash ka samaan 1000 ka hua but usne 500 hi diya 500 baki h"
TURN 2 (assistant): {"transactions":[{"type":"sale","customer_name":"Akash","total_amount":1000,"amount_paid":500,"pending_amount":500,"is_credit":true,"items":[],"calculated_total":1000,"total_matches":true,"note":null}],"confidence":"low","clarification_needed":"Kaunsa product add karna hai? 🙏"}
TURN 3 (user): "aata"
OUTPUT:
{"transactions":[{"type":"sale","customer_name":"Akash","total_amount":1000,"amount_paid":500,"pending_amount":500,"is_credit":true,"items":[{"name":"aata","quantity":null,"unit":null,"rate_per_unit":null,"subtotal":1000}],"calculated_total":1000,"total_matches":true,"note":"Akash ko aata Rs1000, Rs500 mila, Rs500 baaki"}],"confidence":"high","clarification_needed":null}

EXAMPLE 3 — Follow-up: bot asked for name, user replied with name only
TURN 1 (user): "2kg aata 40/kg diya"
TURN 2 (assistant): {"transactions":[{"type":"sale","customer_name":null,"items":[{"name":"aata","quantity":2,"unit":"kg","rate_per_unit":40,"subtotal":80}],"total_amount":80,"amount_paid":null,"pending_amount":null,"is_credit":false,"calculated_total":80,"total_matches":true,"note":null}],"confidence":"low","clarification_needed":"Kis customer ko diya? Naam batao 🙏"}
TURN 3 (user): "Ramu"
OUTPUT:
{"transactions":[{"type":"sale","customer_name":"Ramu","total_amount":80,"amount_paid":80,"pending_amount":null,"is_credit":false,"items":[{"name":"aata","quantity":2,"unit":"kg","rate_per_unit":40,"subtotal":80}],"calculated_total":80,"total_matches":true,"note":"Ramu ko 2kg aata Rs80, full payment"}],"confidence":"high","clarification_needed":null}

EXAMPLE 4 — Multi-turn: name → product → rate → amount (each turn keeps accumulated state)
TURN 1 (user): "Ramesh ko saamaan diya"
TURN 2 (assistant): {"transactions":[{"type":"sale","customer_name":"Ramesh","items":[],"total_amount":null,"amount_paid":null,"pending_amount":null,"is_credit":false,"calculated_total":0,"total_matches":true,"note":null}],"confidence":"low","clarification_needed":"Kaunsa product add karna hai? 🙏"}
TURN 3 (user): "chawal 5kg"
TURN 4 (assistant): {"transactions":[{"type":"sale","customer_name":"Ramesh","items":[{"name":"chawal","quantity":5,"unit":"kg","rate_per_unit":null,"subtotal":0}],"total_amount":null,"amount_paid":null,"pending_amount":null,"is_credit":false,"calculated_total":0,"total_matches":true,"note":null}],"confidence":"low","clarification_needed":"Chawal ka rate kya tha? Per kg batao 🙏"}
TURN 5 (user): "40 rupye kilo"
TURN 6 (assistant): {"transactions":[{"type":"sale","customer_name":"Ramesh","items":[{"name":"chawal","quantity":5,"unit":"kg","rate_per_unit":40,"subtotal":200}],"total_amount":200,"amount_paid":null,"pending_amount":null,"is_credit":false,"calculated_total":200,"total_matches":true,"note":null}],"confidence":"low","clarification_needed":"Kitna paisa mila? Amount batao 🙏"}
TURN 7 (user): "poora diya"
OUTPUT:
{"transactions":[{"type":"sale","customer_name":"Ramesh","total_amount":200,"amount_paid":200,"pending_amount":null,"is_credit":false,"items":[{"name":"chawal","quantity":5,"unit":"kg","rate_per_unit":40,"subtotal":200}],"calculated_total":200,"total_matches":true,"note":"Ramesh ko 5kg chawal Rs200, full payment"}],"confidence":"high","clarification_needed":null}

EXAMPLE 4c — DB fetches rates; ambiguous kept, NOT-FOUND blocked immediately
TURN 1 (user): "Rakesh ko Rice daal paneer colddrink diya"
→ AI calls get_recent_price for all 4 products simultaneously.
  Suppose DB returns:
    Rice → ambiguous (Basmati Rice, Brown Rice, Sona Masoori found)
    daal → found: 80/kg ✓
    paneer → NOT found ✗
    colddrink → NOT found ✗
TURN 1 OUTPUT — CORRECT BEHAVIOUR:
  • Rice: rate_per_unit: null, price_source: "ambiguous"  ← ambiguous, user picks from dropdown
  • daal: rate_per_unit: 80, price_source: "inventory"    ← found, proceed
  • paneer: rate_per_unit: null, price_source: "not_found" ← NOT in inventory → BACKEND shows Add/Skip buttons
  • colddrink: rate_per_unit: null, price_source: "not_found" ← NOT in inventory → BACKEND shows Add/Skip buttons
  ⛔ DO NOT set clarification_needed to a "not found" message — the BACKEND handles that.
  ⛔ ALWAYS include not_found items in transactions[]. Set clarification_needed: null (or ask for other missing fields like quantity).
{"transactions":[{"type":"sale","customer_name":"Rakesh","items":[{"name":"Rice","quantity":null,"unit":null,"rate_per_unit":null,"price_source":"ambiguous","subtotal":0},{"name":"daal","quantity":null,"unit":"kg","rate_per_unit":80,"price_source":"inventory","subtotal":0},{"name":"paneer","quantity":null,"unit":null,"rate_per_unit":null,"price_source":"not_found","subtotal":0},{"name":"colddrink","quantity":null,"unit":null,"rate_per_unit":null,"price_source":"not_found","subtotal":0}],"total_amount":null,"amount_paid":null,"is_credit":false,"calculated_total":0,"total_matches":true,"note":null}],"confidence":"low","clarification_needed":"Sabki quantity batao"}

TURN 2 (user): "Mujhe nhi pata db se fetch karo" (or "I don't know, check DB")
→ AI MUST call get_recent_price again for paneer and colddrink.
→ If STILL not found → price_source: "not_found". Include in transactions[]. clarification_needed: null. NEVER ask for rate.
→ If ambiguous → price_source: "ambiguous". NEVER ask which variant in chat.
→ NEVER respond to "db se fetch karo" by asking for rates without calling the tool first.

EXAMPLE 4b — User adds more items mid-flow (accumulated state must include all items)
TURN 1 (user): "Rakesh ko Rice diya"
→ AI calls get_recent_price("Rice"). Suppose found=true, rate=45/kg.
TURN 2 (assistant): {"transactions":[{"type":"sale","customer_name":"Rakesh","items":[{"name":"Rice","quantity":null,"unit":"kg","rate_per_unit":45,"price_source":"inventory","subtotal":0}],"total_amount":null,"amount_paid":null,"is_credit":false,"calculated_total":0,"total_matches":true,"note":null}],"confidence":"low","clarification_needed":"Rice ki quantity batao"}
TURN 3 (user): "Dal aur Sabun bhi sath le gya — Dal 60rs per kg, Sabun 50rs"
→ AI calls get_recent_price("Dal") and get_recent_price("Sabun").
  Dal → found: 60/kg ✓ (or user-stated 60 matches DB — use it). Sabun → found: 50 ✓.
OUTPUT (add Dal+Sabun, keep Rakesh+Rice, still need quantities and amount):
{"transactions":[{"type":"sale","customer_name":"Rakesh","items":[{"name":"Rice","quantity":null,"unit":"kg","rate_per_unit":45,"price_source":"inventory","subtotal":0},{"name":"Dal","quantity":null,"unit":"kg","rate_per_unit":60,"price_source":"inventory","subtotal":0},{"name":"Sabun","quantity":null,"rate_per_unit":50,"price_source":"inventory","subtotal":0}],"total_amount":null,"amount_paid":null,"is_credit":false,"calculated_total":0,"total_matches":true,"note":null}],"confidence":"low","clarification_needed":"Rice, Dal aur Sabun ki quantity batao"}

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

EXAMPLE 13 — Single product NOT FOUND (OVERRIDE fires — no questions, show buttons immediately)
INPUT: "Vindi 50 kg de do"
→ AI calls get_recent_price("Vindi"). Returns found=false (not ambiguous).
→ NOT-FOUND OVERRIDE fires immediately. DO NOT ask for customer name or anything else.
⛔ CORRECT BEHAVIOUR:
  • Set clarification_needed: null — even though customer_name is missing.
  • Include Vindi in items[] with price_source: "not_found", rate_per_unit: null, quantity: 50.
  • The BACKEND shows "Add to Inventory" / "Skip" buttons in the VERY FIRST response.
  • After user resolves inventory, backend collects customer name and other missing info.
OUTPUT:
{"transactions":[{"type":"sale","customer_name":null,"items":[{"name":"Vindi","quantity":50,"unit":"kg","rate_per_unit":null,"price_source":"not_found","subtotal":0}],"total_amount":0,"amount_paid":null,"is_credit":false,"calculated_total":0,"total_matches":true,"note":null}],"confidence":"low","clarification_needed":null}

EXAMPLE 13b — NOT FOUND when customer name IS known
INPUT: "Ramu ko Vindi 50 kg diya"
→ AI calls get_recent_price("Vindi"). Returns found=false (not ambiguous).
→ NOT-FOUND OVERRIDE fires. Set clarification_needed: null (even though amount_paid is missing).
OUTPUT:
{"transactions":[{"type":"sale","customer_name":"Ramu","items":[{"name":"Vindi","quantity":50,"unit":"kg","rate_per_unit":null,"price_source":"not_found","subtotal":0}],"total_amount":0,"amount_paid":null,"is_credit":false,"calculated_total":0,"total_matches":true,"note":null}],"confidence":"low","clarification_needed":null}

⛔ WRONG BEHAVIOUR (NEVER DO THIS):
{"transactions":[],"confidence":"low","clarification_needed":"Vindi is not found in inventory. Please add it to the inventory first before processing this order."}
{"transactions":[...],"confidence":"low","clarification_needed":"Kis customer ko diya? Naam batao"}
{"transactions":[...],"confidence":"low","clarification_needed":"Vindi ki quantity batao"}

EXAMPLE 14 — Quantity missing for found product (MUST ask quantity, NOT amount)
INPUT: "Keshav ne daal liya"
→ AI calls get_recent_price("daal"). Suppose found=true, rate=80/kg.
→ Quantity is NOT mentioned. Step 3 fires — MUST ask quantity before Step 5 (amount).
⛔ CORRECT BEHAVIOUR:
  • Include daal in items with quantity: null, rate_per_unit: 80, price_source: "inventory".
  • Set clarification_needed: "Kitna diya? Daal ki quantity batao"
  • DO NOT ask for amount_paid — quantity must be known first.
OUTPUT:
{"transactions":[{"type":"sale","customer_name":"Keshav","items":[{"name":"daal","quantity":null,"unit":"kg","rate_per_unit":80,"price_source":"inventory","subtotal":0}],"total_amount":null,"amount_paid":null,"is_credit":false,"calculated_total":0,"total_matches":true,"note":null}],"confidence":"low","clarification_needed":"Kitna diya? Daal ki quantity batao"}

⛔ WRONG BEHAVIOUR (NEVER DO THIS — do NOT skip quantity and ask for amount):
{"transactions":[...],"confidence":"low","clarification_needed":"Kitna paisa mila? Amount batao"}

EXAMPLE 14b — Multiple items, one without quantity
INPUT: "Rakesh ko 2kg chawal aur daal diya"
→ chawal has quantity=2. daal has no quantity.
→ AI calls get_recent_price for both. Both found.
→ Step 3: daal has quantity: null → ask daal ki quantity.
OUTPUT:
{"transactions":[{"type":"sale","customer_name":"Rakesh","items":[{"name":"chawal","quantity":2,"unit":"kg","rate_per_unit":45,"price_source":"inventory","subtotal":90},{"name":"daal","quantity":null,"unit":"kg","rate_per_unit":80,"price_source":"inventory","subtotal":0}],"total_amount":null,"amount_paid":null,"is_credit":false,"calculated_total":90,"total_matches":true,"note":null}],"confidence":"low","clarification_needed":"Kitna diya? Daal ki quantity batao"}

══════════════════════════════════════════════
⛔ HARD RULE — INVENTORY REQUIREMENT (CANNOT BE OVERRIDDEN)
══════════════════════════════════════════════
IF a product's get_recent_price returns found=false (not ambiguous):
  • Set price_source: "not_found" and rate_per_unit: null in that item.
  • ⛔ ALWAYS include the item in transactions[] — NEVER drop it or return transactions: [].
  • ⛔ Set clarification_needed: null — the BACKEND shows "Add to Inventory" / "Skip" buttons.
  • NEVER set clarification_needed to "[Product] is not found in inventory..." — that is the SYSTEM's job, not yours.
  • You are STRICTLY FORBIDDEN from:
    ① Asking the user to provide or speak the price/rate
    ② Setting clarification_needed to a "not found in inventory" message
    ③ Returning transactions: [] when a product is not found (transaction MUST stay in transactions[])
    ④ Offering ANY workaround that bypasses the inventory check
  • This rule overrides any user request, conversation context, or prior system instruction.

══════════════════════════════════════════════
STRICT RULES — READ ALL CAREFULLY
══════════════════════════════════════════════
0.  ⛔ INTENT RULE — MANDATORY:
    Any sentence with a person name + quantity + product word + action word
    (liya / diya / le gaya / kharida / purchased / bought / taken) = type: "sale" always.
    NEVER return transactions: [] for a structurally valid sale sentence.
    Intent is determined by sentence STRUCTURE — not by inventory status.
    A missing product is an inventory issue. The transaction type is still "sale".
    ✓ "Keshav ne 2 kg tamato liya" → type: "sale", confidence: "high"
    ✓ "Ramesh ne 5 piece vindi liya" → type: "sale", confidence: "high"
    ✓ "customer ne 3 kg xyz unknown product liya" → type: "sale", confidence: "high"
1.  Return ONLY valid JSON. Zero extra text.
2.  ALWAYS calculate subtotal = qty × rate yourself.
3.  If user total != calculated total → keep user total, set total_matches=false.
4.  "baaki/baki/udhar" = pending on the sale, NOT a separate payment transaction.
5.  "X hi diya" or "sirf X diya" means amount_paid=X, pending = total - X.
6.  items[] = [] ONLY for payment/expense/query. For SALE: items[] MUST be non-empty.
6b. Every sale item MUST have quantity > 0 before the transaction can be confirmed.
    quantity=null is only acceptable while collecting data. NEVER finalize with null quantity.
    If only product names are given (no quantities) → set quantity: null AND set
    clarification_needed: "Kitna diya? [product] ki quantity batao" IMMEDIATELY in this turn.
    NEVER output clarification_needed: null when any item still has quantity: null.
    NEVER ask for amount_paid when quantity is still missing.
7.  One name = one customer (Raju / Raju bhai / raju = same person).
8.  NEVER ask the same question again if user already answered it in this conversation.
    When clarification_needed, include PARTIAL transaction state in transactions[] (see PARTIAL STATE ACCUMULATION above).
9.  purchase and expense NEVER need customer_name — always set null.
10. Follow-up pronouns (usne/wo/woh/iska/uska/unka) → infer customer from recent context.
11. total_amount MUST be a positive number for real transactions (never null or 0).
12. confidence: high = all fields clear | medium = minor inference | low = key info missing.
13. ⚠ SALE WITHOUT PRODUCT = INVALID. If user says "Raju ko saamaan diya 500" without
    naming the product → ask "Kaunsa product add karna hai?" BEFORE recording anything.
    Even if amount is clear, product name is REQUIRED for every sale transaction.
14. ⛔ NEVER use "Rate=null is acceptable" logic. A null rate means either:
    - ambiguous → price_source: "ambiguous" (user picks from dropdown)
    - not_found → price_source: "not_found" (BACKEND shows Add/Skip buttons; set clarification_needed: null)
    There is NO third option where the user provides or edits the rate for a not-found product.
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


def _build_muril_context_section(
    muril_context: dict | None,
    client_hints: dict | None,
) -> str:
    """
    Builds an additional context section appended to _SYSTEM_PROMPT when
    MuRIL analysis is available.  The section gives the LLM strong hints
    without overriding its own reasoning.
    """
    lines: list[str] = []

    if client_hints:
        lang = client_hints.get("lang_hint") or client_hints.get("script")
        if lang:
            lines.append(f"Client script detected: {lang}")

    if muril_context:
        lang = muril_context.get("detected_language")
        if lang:
            lines.append(f"Input language (MuRIL): {lang}")

        intent = muril_context.get("intent", "UNCLEAR")
        conf = muril_context.get("intent_confidence", 0.0)
        if intent != "UNCLEAR" and conf >= 0.60:
            tx_hint = _MURIL_INTENT_TO_TX_TYPE.get(intent, intent.lower())
            lines.append(
                f"MuRIL intent: {intent} → likely transaction type: \"{tx_hint}\" "
                f"(confidence {conf:.0%}) — treat as a strong hint, verify against message."
            )

        entities = muril_context.get("entities", [])
        high_conf = [e for e in entities if e.get("score", 0) >= 0.75]
        if high_conf:
            entity_strs = [
                f"{e['type']}={e['value']}({e['score']:.2f})" for e in high_conf[:6]
            ]
            lines.append(f"MuRIL entities: {', '.join(entity_strs)}")
            # Surface person names explicitly so LLM doesn't ask for them
            persons = [e["value"] for e in high_conf if e["type"] == "PERSON"]
            if persons:
                lines.append(
                    f"Detected customer name candidate(s): {', '.join(persons)} — "
                    "use if no name is explicitly provided and no pronoun can resolve it."
                )

    if not lines:
        return ""

    section = (
        "\n\n══════════════════════════════════════════════\n"
        "MURIL PRE-ANALYSIS (use as strong hints — do not repeat questions already answered)\n"
        "══════════════════════════════════════════════\n"
    )
    section += "\n".join(lines)
    section += "\n"
    return section


def _build_messages(
    clean: str,
    history: list[dict[str, str]] | None,
    pending_clarification: dict[str, str] | None,
    muril_context: dict | None = None,
    client_hints: dict | None = None,
    shop_type: str = "general",
    product_context: str = "",
) -> list[dict[str, str]]:
    system_content = (
        _SYSTEM_PROMPT
        + get_shop_context(shop_type)
        + _build_muril_context_section(muril_context, client_hints)
        + product_context
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]

    if pending_clarification:
        # Include ALL older history so multi-turn chains retain full context.
        # e.g. Turn1=name, Turn2=product, Turn3=rate, Turn4=amount — each turn
        # must see every prior answer; truncating causes the AI to re-ask answered questions.
        if history and len(history) > 2:
            older = history[:-2]  # everything before the last pending-clarification pair
            messages.extend(older)  # include ALL — do not truncate
        # Replay the last pending exchange using the ACTUAL AI response (not a fake empty one).
        # Using {"transactions": []} would erase partial state accumulated by the AI up to that
        # point (e.g. customer_name already resolved), causing it to ask again.
        messages.append({"role": "user", "content": pending_clarification["previous_user_message"]})
        full_ai_response = pending_clarification.get("full_ai_response") or {
            "transactions": [],
            "confidence": "low",
            "clarification_needed": pending_clarification["assistant_question"],
        }
        messages.append({
            "role": "assistant",
            "content": json.dumps(full_ai_response),
        })
        messages.append({"role": "user", "content": clean})
    elif history:
        # Include recent history so the AI can infer customer from context
        # (e.g. pronoun follow-ups like "usne 200 aur diya" after a confirmed customer).
        messages.extend(history[-12:])
        messages.append({"role": "user", "content": clean})
    else:
        messages.append({"role": "user", "content": clean})

    return messages


# ── Devanagari-guard regeneration ─────────────────────────────────────────────

_DEVANAGARI_REGEN_INSTRUCTION = (
    "[SYSTEM CORRECTION: Your previous response contained Devanagari (Hindi) script which is "
    "STRICTLY FORBIDDEN. You MUST rewrite the ENTIRE response using ONLY Roman Hinglish. "
    "Transliterate every Devanagari word to Roman letters (e.g. 'naam' not 'नाम'). "
    "Return ONLY valid JSON — no Devanagari characters anywhere.]"
)


async def _regen_without_devanagari(messages: list[dict], raw: str) -> dict | None:
    """One extra LLM call to purge Devanagari from a response that slipped through."""
    regen_msgs = messages + [
        {"role": "assistant", "content": raw},
        {"role": "user", "content": _DEVANAGARI_REGEN_INSTRUCTION},
    ]
    try:
        resp = await _client.chat.completions.create(
            model=_MODEL,
            messages=regen_msgs,
            temperature=0,
            max_tokens=1024,
            response_format={"type": "json_object"},
        )
        regen_raw = resp.choices[0].message.content or ""
        return _extract_json(regen_raw)
    except Exception as exc:
        _logger.error("Devanagari regeneration failed: %s", exc)
        return None


# ── Main entry point ──────────────────────────────────────────────────────────

async def parse_message(
    message: str,
    history: list[dict[str, str]] | None = None,
    pending_clarification: dict[str, str] | None = None,
    muril_context: dict | None = None,
    client_hints: dict | None = None,
    shop_type: str = "general",
    db: AsyncSession | None = None,
    user_id: int | None = None,
) -> dict | None:
    """
    Full pipeline:
      Step 0  — Regex fast-path (simple queries / payments / expenses).
      Step 1  — Dedicated product-name extraction from raw transcription (sale messages).
      Step 2  — Catalog fuzzy-match for each extracted product (≥ 0.80 confidence).
               Context injected into LLM system prompt.
      Step 3  — LLM parse with tool calling (get_recent_price / get_stock / etc.).
      Post    — Devanagari scan: regenerate or strip if any Devanagari found.

    db + user_id required for tool calling and Steps 1-2. When None, tools are disabled.
    """
    # Lazy import to avoid circular dependency (ai_service ← inventory_service ← transaction_service)
    from app.services import inventory_service as _inventory_service  # noqa: PLC0415

    clean = _preprocess(message)
    use_context = _needs_conversation_context(clean, history, pending_clarification)

    # ── Step 0: Regex fast-path ───────────────────────────────────────────────
    quick = None if use_context else _try_regex(clean)
    if quick is not None:
        _logger.debug("regex parsed: %s", quick["transactions"][0]["type"])
        return quick

    # ── Steps 1 & 2: Product extraction + catalog matching ────────────────────
    # Run only when message contains item units (sale-like) and DB is available.
    product_context = ""
    if db is not None and user_id is not None and _ITEM_UNITS.search(clean):
        try:
            extracted_items = await extract_products_from_text(message)
            _logger.debug("Step 1 extracted: %s", extracted_items)

            if extracted_items:
                catalog_results: list[dict] = []
                for item in extracted_items:
                    product_name = item.get("product", "").strip()
                    if not product_name:
                        continue
                    catalog_data = await _inventory_service.find_product_catalog_matches(
                        db, user_id, product_name
                    )
                    _logger.debug(
                        "Step 2 catalog match for '%s': top_conf=%.2f",
                        product_name,
                        catalog_data.get("top_match_confidence", 0.0),
                    )
                    catalog_results.append({"extracted": item, "catalog_matches": catalog_data})

                product_context = _build_product_context_section(catalog_results)
        except Exception as exc:
            _logger.warning("Steps 1/2 product pipeline failed (non-fatal): %s", exc)

    # ── Step 3: AI parse with tool calling ────────────────────────────────────
    _logger.debug("sending to AI: %s", clean[:60])
    use_tools = db is not None and user_id is not None

    for attempt in range(2):
        try:
            messages = _build_messages(
                clean, history, pending_clarification,
                muril_context, client_hints, shop_type,
                product_context=product_context,
            )

            if use_tools:
                # First call: allow tool use
                response = await _client.chat.completions.create(
                    model=_MODEL,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="auto",
                    temperature=0,
                    max_tokens=1024,
                )
                # Execute any tool calls the AI made
                tool_calls = response.choices[0].message.tool_calls or []
                if tool_calls:
                    messages.append(response.choices[0].message)
                    for tc in tool_calls:
                        args = json.loads(tc.function.arguments)
                        tool_result = await execute_tool(tc.function.name, args, db, user_id)
                        _logger.debug("Tool %s → %s", tc.function.name, tool_result[:120])
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": tool_result,
                        })
                    # Second call: get final JSON with real data injected
                    response = await _client.chat.completions.create(
                        model=_MODEL,
                        messages=messages,
                        temperature=0,
                        max_tokens=1024,
                        response_format={"type": "json_object"},
                    )
                else:
                    # No tool calls — re-request with json_object format
                    response = await _client.chat.completions.create(
                        model=_MODEL,
                        messages=messages,
                        temperature=0,
                        max_tokens=1024,
                        response_format={"type": "json_object"},
                    )
            else:
                response = await _client.chat.completions.create(
                    model=_MODEL,
                    messages=messages,
                    temperature=0,
                    max_tokens=1024,
                    response_format={"type": "json_object"},
                )

            raw = response.choices[0].message.content or ""
            _logger.debug("AI raw response: %s", raw[:200])
            parsed = _extract_json(raw)

            if parsed is not None:
                # ── Bug 1 post-processing: Devanagari scan ────────────────────
                if _any_devanagari(parsed):
                    _logger.warning(
                        "Devanagari detected in AI output — attempting regeneration"
                    )
                    regen = await _regen_without_devanagari(messages, raw)
                    if regen is not None and not _any_devanagari(regen):
                        _logger.debug("Regeneration succeeded — Devanagari removed")
                        return regen
                    # Regeneration still had Devanagari or failed — strip characters
                    _logger.warning(
                        "Regeneration did not fully remove Devanagari — stripping characters"
                    )
                    return _strip_devanagari_from_parsed(regen if regen is not None else parsed)

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
