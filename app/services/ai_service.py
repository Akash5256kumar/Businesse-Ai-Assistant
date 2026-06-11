from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.shop_context import get_shop_context
from app.services.ai_tools import TOOLS, execute_tool, _normalize_product_name

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
Extract product items from a Hindi/Hinglish/Devanagari shop order.
Return ONLY valid JSON — no extra text.

{"items": [{"product": "<clean_name>", "quantity": <number_or_null>, "unit": "<unit_or_null>"}]}

Rules for product:
  - Lowercase only
  - Strip ONLY filler/action words: bhai, de do, wala, please, yaar, dena, lena, dijiye,
    chahiye, hai, ko, ne, ka, ki, ke, liya, diya, le gaya, aur, and, etc.
  - Strip pronouns and pure verbs — but NEVER strip brand names or product model names.
  - Keep the FULL product name — brand prefix + variety name + model suffix.
    e.g. "Delhi Pasand Easy" → product: "delhi pasand easy"  (keep all 3 words)
         "Galaxy 1121"       → product: "galaxy 1121"        (keep brand + model number)
         "Biryani King No.1 Jammuni" → product: "biryani king no.1 jammuni"

  ⛔ BRAND NAME RULE — CRITICAL:
     Brand names are NOT customer names. NEVER strip brand words.
     Words like "Delhi Pasand", "Zeeba", "Patanjali", "India Gate", "Aeroplane",
     "Kitchen Champion", "Patliputra Farm", "Biryani King" etc. are product brands — keep them.
     A customer name appears BEFORE "ne/ko" in the sentence. The product name appears AFTER.
     ✓ "Keshav ne Delhi Pasand Easy 5kg liya" → customer=Keshav, product="delhi pasand easy"
     ✓ "Rahul ko Galaxy 1121 10kg diya"       → customer=Rahul, product="galaxy 1121"

  ⛔ ABBREVIATION RULE:
     Short brand codes are valid product names — keep them verbatim.
     ✓ "dp easy 5kg"     → product: "dp easy"     (dp = brand abbreviation)
     ✓ "bk saffron 10kg" → product: "bk saffron"  (bk = brand abbreviation)
     ✓ "ig kolam 25kg"   → product: "ig kolam"    (ig = brand abbreviation)
     NEVER expand or guess what an abbreviation means — pass it through as-is.

  ⛔ VERBATIM EXTRACTION — ABSOLUTE RULE (applies to ALL input including Devanagari):
     Extract the product name EXACTLY as the user said it. Do NOT rename, translate, or
     substitute with a "more common" product name from your training knowledge.
     The inventory system will identify the correct match — your job is ONLY to extract verbatim.
  ✓ "minket rice"       → product: "minket rice"       ⛔ NOT "brown rice" or "white rice"
  ✓ "mansouri rice"     → product: "mansouri rice"      ⛔ NOT "masoori rice" or "brown rice"
  ✓ "yellow rice"       → product: "yellow rice"        ⛔ NOT "basmati rice"
  ✓ "rajbhog rice"      → product: "rajbhog rice"       ⛔ NOT "basmati rice"
  ✓ "mogra rice"        → product: "mogra rice"         ⛔ NOT "broken rice" or "white rice"
  ✓ "trade rice"        → product: "trade rice"         ⛔ NOT "brown rice"
  ✓ "mingat rice"       → product: "mingat rice"        ⛔ NOT "minket rice" or "brown rice"
  ✓ "wada kolam rice"   → product: "wada kolam rice"    ⛔ NOT "kolam rice" or "brown rice"
  ✓ "zeeba classic"     → product: "zeeba classic"      ⛔ NOT "classic rice" or "basmati rice"
  ✓ "delhi pasand easy" → product: "delhi pasand easy"  ⛔ NOT "easy rice" or "rice"
  ✓ "galaxy 1121"       → product: "galaxy 1121"        ⛔ NOT "basmati rice" or "1121 rice"
  If you don't recognise a product name → TRANSLITERATE it phonetically. Never substitute.

⚠ DEVANAGARI TRANSLITERATION — phonetic only, ZERO substitution allowed:
  Transliterate Devanagari words to Roman script phonetically. NEVER substitute with similar names.
  "मिंगट"   → "mingat"    (NOT "minket", NOT "brown rice")
  "ट्रेड"    → "trade"     (NOT "brown rice")
  "वाडा"    → "wada"      (NOT "vada" — but either is OK)
  "कोलंब"   → "kolam"     (NOT "kolumb")
  "मूंग"    → "moong"     (NOT "mooch", NOT "mung")
  "मसूरी"   → "masuri"    (NOT "masoori" — but close is OK)
  "काली"    → "kali"
  "मोंस"    → "mons"      (NOT "moong", NOT "mooch", NOT "brown", NOT "mons rice")
  "श्रीराम"  → "sriram"
  "ज़ीबा"   → "zeeba"     (NOT "jeba" or "jiba")
  "दिल्ली पसंद" → "delhi pasand"  (brand name — keep full)

  ⛔ UNKNOWN DEVANAGARI WORD — MANDATORY FALLBACK RULE:
     If you cannot identify a Devanagari word → break it into characters and transliterate phonetically.
     Devanagari character map: म=m, ो=o, ं=n(nasal/anusvara), स=s, क=k, ा=a, ल=l, ि=i, र=r
     Example: "मोंस" → म+ो+ं+स = m+o+n+s = "mons"
     ⛔ FORBIDDEN: guessing semantics of an unknown word, e.g.:
        "काली मोंस" ≠ "brown rice"   (काली=kali, मोंस=mons → "kali mons")
        "काली मोंस" ≠ "black rice"   (काली is a product-name prefix here, NOT a color descriptor)
        Unknown word + राइस ≠ "brown rice"  (only substitute if ब्राउन appears in the text)

Rules for quantity:
  - Extract the numeric value (e.g. "5 kilo" → 5, "2 dozen" → 2)
  - Spoken Hindi number words → convert to digits:
    five/paanch/पाँच=5  seven/saat/सात=7  ten/das/दस=10  fifteen/pandrah/पंद्रह=15
    twenty/bees/बीस=20  fifty/pachaas/पचास=50  hundred/sow/sau/सौ=100
    Compound: "सट्टाईस सौ पचास" = 27×100+50 = 2750  |  "saat sow pachaas" = 750
  - null if no quantity mentioned

Rules for unit:
  - Normalize: kilo/kilogram/केजी/किलो → kg | gram/grams → g | litre/liter/ltr → litre
  - piece/pcs/pc → piece | dozen → dozen | packet/pack → packet
  - null if no unit mentioned

Examples (Latin input — generic products):
"bhai 5 kilo wala atta de do" → {"items":[{"product":"atta","quantity":5,"unit":"kg"}]}
"Raju ko 2kg chawal 1kg daal diya" → {"items":[{"product":"chawal","quantity":2,"unit":"kg"},{"product":"daal","quantity":1,"unit":"kg"}]}
"paneer dena" → {"items":[{"product":"paneer","quantity":null,"unit":null}]}
"rice, sugar, aata leke gaya — 5kg each" → {"items":[{"product":"rice","quantity":5,"unit":"kg"},{"product":"sugar","quantity":5,"unit":"kg"},{"product":"aata","quantity":5,"unit":"kg"}]}
"Keshav ne rajbhog rice liya five kg, seven kg minket rice liya" → {"items":[{"product":"rajbhog rice","quantity":5,"unit":"kg"},{"product":"minket rice","quantity":7,"unit":"kg"}]}

Examples (Latin input — brand / multi-word products):
"Ramesh ko Delhi Pasand Easy 5kg aur Zeeba Classic 10kg diya" → {"items":[{"product":"delhi pasand easy","quantity":5,"unit":"kg"},{"product":"zeeba classic","quantity":10,"unit":"kg"}]}
"dp easy 5kg aur dp light 10kg liya" → {"items":[{"product":"dp easy","quantity":5,"unit":"kg"},{"product":"dp light","quantity":10,"unit":"kg"}]}
"Galaxy 1121 basmati 25kg bheja" → {"items":[{"product":"galaxy 1121","quantity":25,"unit":"kg"}]}
"Biryani King Jammuni 50kg aur Gulmehak 1121 20kg" → {"items":[{"product":"biryani king jammuni","quantity":50,"unit":"kg"},{"product":"gulmehak 1121","quantity":20,"unit":"kg"}]}
"ig kolam 15kg aur bk saffron 5kg diya" → {"items":[{"product":"ig kolam","quantity":15,"unit":"kg"},{"product":"bk saffron","quantity":5,"unit":"kg"}]}
"Patliputra Farm Katarni Steam 30kg liya" → {"items":[{"product":"patliputra farm katarni steam","quantity":30,"unit":"kg"}]}
"zeeba white sela dubar 10kg zeeba xxxl biryani 20kg" → {"items":[{"product":"zeeba white sela dubar","quantity":10,"unit":"kg"},{"product":"zeeba xxxl biryani","quantity":20,"unit":"kg"}]}

Examples (Devanagari — transliterate verbatim, do NOT substitute):
"केशव ने 6 केजी मिंगट राइस लिया" → {"items":[{"product":"mingat rice","quantity":6,"unit":"kg"}]}
"15 केजी ट्रेड राइस दिया" → {"items":[{"product":"trade rice","quantity":15,"unit":"kg"}]}
"श्रीराम वाडा कोलंब राइस 6 केजी" → {"items":[{"product":"sriram wada kolam rice","quantity":6,"unit":"kg"}]}
"5 केजी काली मूंग चावल" → {"items":[{"product":"kali moong chawal","quantity":5,"unit":"kg"}]}
"सोना मसूरी राइस 15 केजी" → {"items":[{"product":"sona masuri rice","quantity":15,"unit":"kg"}]}
"27 केजी ब्राउन राइस" → {"items":[{"product":"brown rice","quantity":27,"unit":"kg"}]}
"4 केजी ब्लैक राइस" → {"items":[{"product":"black rice","quantity":4,"unit":"kg"}]}
"5 केजी काली मोंस राइस" → {"items":[{"product":"kali mons rice","quantity":5,"unit":"kg"}]}
"10 केजी मोंस राइस दिया" → {"items":[{"product":"mons rice","quantity":10,"unit":"kg"}]}
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

        if top_conf >= 0.90 and match_list:
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
            qty_json = str(qty) if qty is not None else "null"
            unit_json = f'"{unit}"' if unit else "null"
            lines.append(
                f"    → ⛔ NOT FOUND ({top_conf:.0%}) — NOT in inventory database.\n"
                f"    ⚠ COPY THIS EXACT ITEM into transactions[].items[] (do NOT modify name or values):\n"
                f"      {{\"name\": \"{product}\", \"quantity\": {qty_json}, \"unit\": {unit_json}, "
                f"\"rate_per_unit\": null, \"price_source\": \"not_found\", \"subtotal\": 0}}\n"
                f"    ⛔ DO NOT call get_recent_price for '{product}' — already resolved: not_found.\n"
                f"    ⛔ DO NOT rename '{product}' to 'brown rice', 'white rice', or any other name.\n"
                f"    → BACKEND handles Add/Skip buttons. Set clarification_needed: null."
            )

    lines.append("")
    return "\n".join(lines)


# ── Devanagari substitution guard ─────────────────────────────────────────────
# Step 1 LLM sometimes replaces unknown Devanagari words with generic English
# product names (e.g. "काली मोंस" → "brown rice"). When this happens Step 2
# confirms the wrong name with 100% confidence and the error propagates silently.
# This filter discards any catalog pre-analysis entry whose extracted product name
# is a known substitution target but whose required Devanagari/Latin indicator is
# absent from the original message.  Without a pre-analysis entry the Step 3 AI
# must call get_recent_price(), which goes through aliases + fuzzy matching.
_DEVANAGARI_SUBSTITUTION_GUARDS: dict[str, list[str]] = {
    "brown rice":   ["ब्राउन", "brown"],
    "white rice":   ["व्हाइट", "वाइट", "white"],
    "black rice":   ["ब्लैक", "black"],
    "basmati rice": ["बासमती", "basmati"],
}


def _filter_devanagari_substitutions(
    catalog_results: list[dict], original: str
) -> list[dict]:
    if not _DEVANAGARI_RE.search(original):
        return catalog_results
    lower_orig = original.lower()
    filtered = []
    for res in catalog_results:
        product = (res.get("extracted", {}).get("product") or "").lower().strip()
        required = _DEVANAGARI_SUBSTITUTION_GUARDS.get(product)
        if required is not None and not any(ind.lower() in lower_orig for ind in required):
            _logger.debug("substitution guard: dropping '%s' from pre-analysis", product)
            continue
        filtered.append(res)
    return filtered


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

⛔ PRODUCT NAME INTEGRITY — HARDEST RULE (read before every tool call):
  When calling get_recent_price, use the EXACT product name the user said.
  NEVER rename, translate, substitute, or "correct" a product name using your training knowledge.
  The inventory system handles matching — your job is to pass the verbatim name.
  ✓ User says "minket rice"    → call get_recent_price("minket rice")
  ✓ User says "mansouri rice"  → call get_recent_price("mansouri rice")
  ✓ User says "yellow rice"    → call get_recent_price("yellow rice")
  ✓ User says "rajbhog"        → call get_recent_price("rajbhog")
  ⛔ NEVER call get_recent_price("brown rice") when user said "minket rice"
  ⛔ NEVER call get_recent_price("basmati rice") when user said "mansouri rice"
  Substituting product names causes wrong prices and corrupts the transaction record.

  ⛔ DEVANAGARI PRODUCT NAMES — phonetic transliteration, never semantic translation:
     If the user's message contains Devanagari script, call get_recent_price with the
     PHONETIC Roman transliteration of what was said — NEVER semantically interpret it.
     ✓ "काली मोंस राइस"  → call get_recent_price("kali mons rice")   ⛔ NOT "brown rice"
     ✓ "मंसूरी राइस"     → call get_recent_price("mansouri rice")     ⛔ NOT "masoori rice"
     "काली" is a product-name prefix (transliterates to "kali"), NOT the English word "black" or "brown".
     Unknown Devanagari word + राइस (rice) does NOT mean "brown rice" — transliterate it phonetically.

⛔ HARD INVENTORY RULE — read carefully:
  A sale can ONLY proceed when EVERY product is confirmed to exist in the inventory database.
  • If a product is NOT found → you MUST NOT collect further info for that product.
  • Do NOT ask the user for price, rate, or suggest "set it in the edit screen".
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
     STRICTLY FORBIDDEN: asking for price, mentioning "edit screen", setting clarification_needed to any "not found" message.

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
  • If user has NOT explicitly stated how much was paid → ALWAYS ask: "Kitna paisa diya? Amount batao 💰"
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
                          NEVER ask user for rate. NEVER suggest edit screen for not-found products.
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
For SALE (STRICT — ALL 4 are mandatory before recording):
  Step 1 — customer_name missing AND not inferable → ask customer name FIRST
  Step 2 — product/item name missing OR items[] is empty → ask "Kaunsa product add karna hai? 🙏" (MANDATORY)
  Step 3 — quantity missing for ANY item → ask quantity for those items
             Example: "Kitna diya? Rice, daal, paneer ki quantity batao (kg/litre/piece)"
             Note: NEVER ask for rate — DB fetches it automatically via get_recent_price.
  Step 4 — rate_per_unit missing for ANY item → call get_recent_price tool FIRST.
             If found=true → use returned rate, price_source: "inventory". ⛔ DO NOT ask user for rate.
             If found=false AND ambiguous → price_source: "ambiguous", rate_per_unit: null. ⛔ DO NOT ask user for rate.
             If found=false (not ambiguous) → price_source: "not_found", rate_per_unit: null.
             ⛔ For not_found: set clarification_needed: null. ALWAYS include in transactions[].
             The BACKEND detects not_found items and shows Add/Skip buttons automatically.
             ⛔ NEVER ask user for rate in ANY case. NEVER ask "rate kya tha?" or "per kg batao".
  Step 5 — amount_paid missing → ask "Kitna paisa diya? Amount batao 💰"

QUANTITY RULES — MANDATORY:
  • quantity MUST be a positive number for every item in a sale.
  • NEVER record a sale item with quantity=null or quantity=0.
  • If user gives product names but no quantities → set quantity: null and ask.
  • quantity and rate can be asked TOGETHER in one message to save turns.
  • Once quantity is known, calculate subtotal = quantity × rate_per_unit.

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

EXAMPLE 4 — Multi-turn: name → product → amount (rate auto-fetched from DB, NEVER asked from user)
TURN 1 (user): "Ramesh ko saamaan diya"
TURN 2 (assistant): {"transactions":[{"type":"sale","customer_name":"Ramesh","items":[],"total_amount":null,"amount_paid":null,"pending_amount":null,"is_credit":false,"calculated_total":0,"total_matches":true,"note":null}],"confidence":"low","clarification_needed":"Kaunsa product add karna hai? 💰"}
TURN 3 (user): "chawal 5kg"
→ AI calls get_recent_price("chawal") → found: rate_per_unit=40, unit="kg"
⛔ DO NOT ask "Chawal ka rate kya tha?" — rate came from DB. Proceed directly to amount.
TURN 4 (assistant): {"transactions":[{"type":"sale","customer_name":"Ramesh","items":[{"name":"chawal","quantity":5,"unit":"kg","rate_per_unit":40,"subtotal":200,"price_source":"inventory"}],"total_amount":200,"amount_paid":null,"pending_amount":null,"is_credit":false,"calculated_total":200,"total_matches":true,"note":null}],"confidence":"low","clarification_needed":"Kitna paisa diya? Amount batao 💰"}
TURN 5 (user): "poora diya"
OUTPUT:
{"transactions":[{"type":"sale","customer_name":"Ramesh","total_amount":200,"amount_paid":200,"pending_amount":null,"is_credit":false,"items":[{"name":"chawal","quantity":5,"unit":"kg","rate_per_unit":40,"subtotal":200,"price_source":"inventory"}],"calculated_total":200,"total_matches":true,"note":"Ramesh ko 5kg chawal Rs200, full payment"}],"confidence":"high","clarification_needed":null}

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

EXAMPLE 13 — Single product NOT FOUND, no customer name yet
INPUT: "Vindi 50 kg de do"
→ AI calls get_recent_price("Vindi"). Returns found=false (not ambiguous).
⛔ CORRECT BEHAVIOUR:
  • Include Vindi in items[] with price_source: "not_found", rate_per_unit: null.
  • Set clarification_needed: null — do NOT write "Vindi is not found in inventory".
  • The BACKEND shows "Add to Inventory" / "Skip" buttons automatically.
OUTPUT:
{"transactions":[{"type":"sale","customer_name":null,"items":[{"name":"Vindi","quantity":50,"unit":"kg","rate_per_unit":null,"price_source":"not_found","subtotal":0}],"total_amount":0,"amount_paid":null,"is_credit":false,"calculated_total":0,"total_matches":true,"note":null}],"confidence":"low","clarification_needed":null}

⛔ WRONG BEHAVIOUR (NEVER DO THIS):
{"transactions":[],"confidence":"low","clarification_needed":"Vindi is not found in inventory. Please add it to the inventory first before processing this order."}

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
    If only product names are given (no quantities), set quantity: null and ask immediately.
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

# Spoken Hindi number words → digit strings (used in _preprocess).
# Applied in order: higher-value words first so "bees" isn't eaten before "biswan".
_HINDI_ONES = {
    "ek":"1","do":"2","teen":"3","char":"4","paanch":"5","chhe":"6","saat":"7",
    "aath":"8","nau":"9","das":"10","gyarah":"11","barah":"12","terah":"13",
    "chaudah":"14","pandrah":"15","solah":"16","satrah":"17","atharah":"18",
    "unnis":"19","bees":"20","ikkees":"21","baais":"22","teis":"23","chaubees":"24",
    "pachees":"25","chhabbees":"26","sattaais":"27","athaais":"28","untees":"29",
    "tees":"30","ikattees":"31","battees":"32","taintees":"33","chautees":"34",
    "paintees":"35","chhattees":"36","saintees":"37","artees":"38","untaalis":"39",
    "chaalees":"40","ikatalis":"41","bayalis":"42","taintalis":"43","chawalis":"44",
    "paintalis":"45","chhiyalis":"46","saintalis":"47","artalis":"48","unchaas":"49",
    "pachaas":"50","ikyawan":"51","baawan":"52","tirpan":"53","chawwan":"54",
    "pachpan":"55","chhappan":"56","sattawan":"57","athawan":"58","unsath":"59",
    "saath":"60","iksath":"61","basath":"62","tirsath":"63","chausath":"64",
    "painsath":"65","chhiyasath":"66","sarsath":"67","arsath":"68","unhattar":"69",
    "sattar":"70","ikattar":"71","bahattar":"72","tihattar":"73","chauhattar":"74",
    "pachhattar":"75","chhihattar":"76","satattar":"77","atattar":"78","unasi":"79",
    "assi":"80","ikyaasi":"81","bayaasi":"82","tiraasi":"83","chauraasi":"84",
    "pachaasi":"85","chhiyaasi":"86","sataasi":"87","ataasi":"88","nawaasi":"89",
    "nabbe":"90","ikyaanwe":"91","baanwe":"92","tiraanwe":"93","chauraanwe":"94",
    "panchaanwe":"95","chhiyaanwe":"96","sataanwe":"97","ataanwe":"98","ninyaanwe":"99",
}
_HINDI_SCALE = {
    r"\bsou?\b": "100", r"\bsow\b": "100", r"\bsauw\b": "100",
    r"\bhazar\b": "1000", r"\bhazaar\b": "1000",
    r"\blakh\b": "100000", r"\blac\b": "100000",
}


def _resolve_spoken_numbers(text: str) -> str:
    """
    Collapse number sequences produced by Hindi word→digit substitution.
    Handles the most common spoken patterns used in Indian retail:
      "27 100 50"  → 2750   (sattaais sow pachaas)
      "7 100 50"   → 750    (saat sow pachaas)
      "3 1000"     → 3000   (teen hazaar)
      "3 1000 500" → 3500   (teen hazaar paanch sow)
    Applied repeatedly until stable so nested compounds collapse fully.
    """
    for _ in range(4):
        prev = text
        # N × 1000 + M × 100 + R  (e.g. "3 1000 7 100 50" → 3750)
        text = re.sub(
            r"\b(\d+)\s+1000\s+(\d+)\s+100\s+(\d+)\b",
            lambda m: str(int(m.group(1))*1000 + int(m.group(2))*100 + int(m.group(3))),
            text,
        )
        # N × 1000 + implicit 1×100 + R  (e.g. "3 1000 100 50" → 3150)
        text = re.sub(
            r"\b(\d+)\s+1000\s+100\s+(\d+)\b",
            lambda m: str(int(m.group(1))*1000 + 100 + int(m.group(2))),
            text,
        )
        # N × 1000 + implicit 1×100  (e.g. "3 1000 100" → 3100)
        text = re.sub(
            r"\b(\d+)\s+1000\s+100\b",
            lambda m: str(int(m.group(1))*1000 + 100),
            text,
        )
        # N × 1000 + R  (e.g. "3 1000 50" → 3050)
        text = re.sub(
            r"\b(\d+)\s+1000\s+(\d+)\b",
            lambda m: str(int(m.group(1))*1000 + int(m.group(2))),
            text,
        )
        # N × 1000  (e.g. "3 1000" → 3000)
        text = re.sub(
            r"\b(\d+)\s+1000\b",
            lambda m: str(int(m.group(1))*1000),
            text,
        )
        # N × 100 + R  (e.g. "27 100 50" → 2750)
        text = re.sub(
            r"\b(\d+)\s+100\s+(\d+)\b",
            lambda m: str(int(m.group(1))*100 + int(m.group(2))),
            text,
        )
        # N × 100  (e.g. "27 100" → 2700)
        text = re.sub(
            r"\b(\d+)\s+100\b",
            lambda m: str(int(m.group(1))*100),
            text,
        )
        if text == prev:
            break
    return text


def _preprocess(message: str) -> str:
    text = message.strip()
    text = re.sub(r"₹\s*", "Rs ", text)
    text = re.sub(r"\b(rs\.?|inr)\s*", "Rs ", text, flags=re.IGNORECASE)
    # Convert spoken Hindi number words to digits
    for word, digit in sorted(_HINDI_ONES.items(), key=lambda x: -len(x[0])):
        text = re.sub(rf"\b{word}\b", digit, text, flags=re.IGNORECASE)
    for pattern, digit in _HINDI_SCALE.items():
        text = re.sub(pattern, digit, text, flags=re.IGNORECASE)
    # Collapse "27 100 50" → "2750" etc.
    text = _resolve_spoken_numbers(text)
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


# ── Product name substitution guard ──────────────────────────────────────────

def _fix_substituted_product_names(parsed: dict, catalog_results: list[dict]) -> dict:
    """
    Detect and reverse product name substitutions in the AI response.

    Problem: When products are not in the inventory the AI sometimes renames them
    to a known product (e.g. "mingat rice" → "brown rice") instead of keeping the
    original name with price_source: "not_found".

    Strategy: compare each AI-returned item (price_source="inventory") against the
    Step-1-extracted products that had no catalog match (confidence < 0.50). When
    quantities match AND names are different, the AI substituted — restore the
    original extracted name and mark the item as not_found.
    """
    not_found_extracted = [
        r["extracted"] for r in catalog_results
        if r["catalog_matches"].get("top_match_confidence", 0.0) < 0.50
    ]
    if not not_found_extracted:
        return parsed

    # Build (name, qty) pairs that are legitimately found in inventory so we never
    # accidentally overwrite a correct match.
    found_pairs: set[tuple] = set()
    for r in catalog_results:
        if r["catalog_matches"].get("top_match_confidence", 0.0) >= 0.90:
            ext = r["extracted"]
            ext_name = (ext.get("product") or "").lower()
            ext_qty = ext.get("quantity")
            found_pairs.add((ext_name, ext_qty))
            match_list = r["catalog_matches"].get("matches", [])
            if match_list:
                found_pairs.add((match_list[0].get("product_name", "").lower(), ext_qty))

    for tx in parsed.get("transactions", []):
        items = tx.get("items") or []
        used_not_found: set[str] = set()

        for item in items:
            if item.get("price_source") != "inventory":
                continue
            ai_name = (item.get("name") or "").lower().strip()
            item_qty = item.get("quantity")
            if item_qty is None:
                continue

            # Skip legitimately found products
            if (ai_name, item_qty) in found_pairs:
                continue

            item_qty_f = float(item_qty)

            for ext in not_found_extracted:
                ext_name = (ext.get("product") or "").lower()
                if ext_name in used_not_found:
                    continue
                ext_qty = ext.get("quantity")
                if ext_qty is None:
                    continue
                if abs(float(ext_qty) - item_qty_f) > 0.01:
                    continue
                # Names must be genuinely different (not just minor casing/spacing)
                if ext_name in ai_name or ai_name in ext_name:
                    continue
                # AI substituted — restore original name
                _logger.warning(
                    "Substitution guard: restored '%s' qty=%s → '%s' (not_found)",
                    ai_name, item_qty, ext_name,
                )
                item["name"] = ext_name
                item["rate_per_unit"] = None
                item["price_source"] = "not_found"
                item["subtotal"] = 0
                used_not_found.add(ext_name)
                break

    return parsed


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
      Step 2  — Catalog fuzzy-match for each extracted product (≥ 0.90 → FOUND, else AMBIGUOUS).
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
    catalog_results: list[dict] = []  # kept for post-processing validation
    if db is not None and user_id is not None and _ITEM_UNITS.search(clean):
        try:
            extracted_items = await extract_products_from_text(message)
            _logger.debug("Step 1 extracted: %s", extracted_items)

            if extracted_items:
                _catalog_results: list[dict] = []
                for item in extracted_items:
                    product_name = item.get("product", "").strip()
                    if not product_name:
                        continue
                    # Normalize via alias library before catalog lookup so Step 2
                    # benefits from the same aliases as Step 3's get_recent_price call.
                    normalized = _normalize_product_name(product_name)
                    catalog_data = await _inventory_service.find_product_catalog_matches(
                        db, user_id, normalized
                    )
                    _logger.debug(
                        "Step 2 catalog match for '%s' (normalized: '%s'): top_conf=%.2f",
                        product_name,
                        normalized,
                        catalog_data.get("top_match_confidence", 0.0),
                    )
                    _catalog_results.append({"extracted": item, "catalog_matches": catalog_data})

                catalog_results = _filter_devanagari_substitutions(_catalog_results, message)
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
                # ── Product name substitution guard ───────────────────────────
                if catalog_results:
                    parsed = _fix_substituted_product_names(parsed, catalog_results)

                # ── Bug 1 post-processing: Devanagari scan ────────────────────
                if _any_devanagari(parsed):
                    _logger.warning(
                        "Devanagari detected in AI output — attempting regeneration"
                    )
                    regen = await _regen_without_devanagari(messages, raw)
                    if regen is not None and not _any_devanagari(regen):
                        _logger.debug("Regeneration succeeded — Devanagari removed")
                        if catalog_results:
                            regen = _fix_substituted_product_names(regen, catalog_results)
                        return regen
                    # Regeneration still had Devanagari or failed — strip characters
                    _logger.warning(
                        "Regeneration did not fully remove Devanagari — stripping characters"
                    )
                    stripped = _strip_devanagari_from_parsed(regen if regen is not None else parsed)
                    if catalog_results:
                        stripped = _fix_substituted_product_names(stripped, catalog_results)
                    return stripped

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
