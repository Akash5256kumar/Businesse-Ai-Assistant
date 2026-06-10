from __future__ import annotations

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import inventory_service

_logger = logging.getLogger(__name__)

# ── Product name normalization ─────────────────────────────────────────────────
#
# Two-layer lookup:
#   1. _PRODUCT_ALIASES  — exact phrase → canonical name
#      Handles Hindi names, regional names, brand names, and known spellings.
#   2. _WORD_SUBS        — word-by-word substitution for compound phrases
#      Converts "basmati chawal" → "basmati rice" when "chawal"→"rice" is in the table.
#      The result is then re-checked against _PRODUCT_ALIASES.
#
# After normalization the name is passed to inventory_service where _match_score
# handles any remaining character-level typos (e.g. "basmti rice" → "basmati rice").

_PRODUCT_ALIASES: dict[str, str] = {
    # ── Vegetables ────────────────────────────────────────────────────────────
    "tamato": "tomato", "tamatar": "tomato", "tamaatar": "tomato",
    "vindi": "bhindi", "bindi": "bhindi",
    "aaloo": "aloo", "alu": "aloo",
    "gobhi": "gobi", "gobhee": "gobi",
    "matar": "mutter", "matter": "mutter", "mattar": "mutter",
    "baigan": "baingan", "brinjal": "baingan",
    "pyaz": "onion", "pyaaz": "onion",
    "adrak": "ginger", "lahsun": "garlic",
    "dhania": "coriander", "dhaniya": "coriander",
    "mirchi": "chilli", "hari mirchi": "green chilli",
    "nimbu": "lemon", "nimbo": "lemon",

    # ── Generic rice / chawal ──────────────────────────────────────────────────
    "chawal": "rice", "chaawal": "rice", "chavel": "rice", "chawl": "rice",
    "chawal rice": "rice",

    # ── Basmati Rice ──────────────────────────────────────────────────────────
    "basmati": "basmati rice",
    "basmaati": "basmati rice", "basmti": "basmati rice",
    "basmati chawal": "basmati rice", "basmti chawal": "basmati rice",
    "long grain rice": "basmati rice",

    # ── Aged / Extra-aged Basmati ─────────────────────────────────────────────
    "aged basmati": "aged basmati rice",
    "extra aged basmati": "aged basmati rice",

    # ── Mini Basmati ──────────────────────────────────────────────────────────
    "mini basmati": "mini basmati rice",
    "mini basmti": "mini basmati rice",
    "mini basmati chawal": "mini basmati rice",

    # ── Rajbhog Rice ─────────────────────────────────────────────────────────
    "rajbhog": "rajbhog rice", "raj bhog": "rajbhog rice",
    "rajbhog chawal": "rajbhog rice", "raj bhog chawal": "rajbhog rice",

    # ── Dubar Rice ───────────────────────────────────────────────────────────
    "dubar": "dubar rice", "dubra": "dubar rice", "dubara": "dubar rice",
    "dubar chawal": "dubar rice", "dubra chawal": "dubar rice",

    # ── Rozana Rice ──────────────────────────────────────────────────────────
    "rozana": "rozana rice", "rosana": "rozana rice",
    "rozana chawal": "rozana rice",

    # ── Sona Masoori Rice ─────────────────────────────────────────────────────
    "sona masoori": "sona masoori rice", "sona masuri": "sona masoori rice",
    "sona masoor": "sona masoori rice", "sonamasoori": "sona masoori rice",
    "sonamasuri": "sona masoori rice", "sona masuri rice": "sona masoori rice",
    "sona masoori chawal": "sona masoori rice",
    "sona masuri chawal": "sona masoori rice",
    # Spoken/regional variants of Masoori (mansouri, mansuri, masuri)
    "mansouri": "sona masoori rice", "mansouri rice": "sona masoori rice",
    "mansouri chawal": "sona masoori rice",
    "mansuri": "sona masoori rice", "mansuri rice": "sona masoori rice",
    "mansoor rice": "sona masoori rice", "masuri rice": "sona masoori rice",

    # ── Kolam Rice ───────────────────────────────────────────────────────────
    "kolam": "kolam rice", "kolam chawal": "kolam rice",
    "lachkari kolam": "lachkari kolam rice",
    "lachkari kolam chawal": "lachkari kolam rice",

    # ── Jeera Rice ───────────────────────────────────────────────────────────
    "jeera rice": "jeera rice", "jeera chawal": "jeera rice",
    "jeerakasala": "jeerakasala rice", "jeera kasala": "jeerakasala rice",

    # ── Gobindobhog Rice ──────────────────────────────────────────────────────
    "gobindobhog": "gobindobhog rice", "gobindo bhog": "gobindobhog rice",
    "gobindobhog chawal": "gobindobhog rice",

    # ── HMT Rice ─────────────────────────────────────────────────────────────
    "hmt": "hmt rice", "hmt chawal": "hmt rice",

    # ── IR64 Rice ────────────────────────────────────────────────────────────
    "ir64": "ir64 rice", "ir 64": "ir64 rice", "ir64 chawal": "ir64 rice",

    # ── Swarna Rice ──────────────────────────────────────────────────────────
    "swarna": "swarna rice", "swarna chawal": "swarna rice",

    # ── Ponni Rice ───────────────────────────────────────────────────────────
    "ponni": "ponni rice", "ponni chawal": "ponni rice",

    # ── Matta Rice ───────────────────────────────────────────────────────────
    "matta": "matta rice", "matta chawal": "matta rice",
    "kerala matta": "matta rice",

    # ── Sharbati Rice ─────────────────────────────────────────────────────────
    "sharbati": "sharbati rice", "sharbati chawal": "sharbati rice",
    "shabati": "sharbati rice", "shabati rice": "sharbati rice",

    # ── Sugandha Rice ─────────────────────────────────────────────────────────
    "sugandha": "sugandha rice", "sugandha chawal": "sugandha rice",

    # ── Mogra Rice ───────────────────────────────────────────────────────────
    "mogra": "mogra rice", "mogra chawal": "mogra rice",

    # ── Tukda / Broken Rice ───────────────────────────────────────────────────
    "tukda": "tukda rice", "tukda chawal": "tukda rice",
    "broken rice": "tukda rice", "tuta chawal": "tukda rice",

    # ── Golden Sella / White Sella ────────────────────────────────────────────
    "golden sella": "golden sella rice", "golden sella chawal": "golden sella rice",
    "white sella": "white sella rice", "white sella chawal": "white sella rice",

    # ── Brown Rice ───────────────────────────────────────────────────────────
    "brown rice": "brown rice", "brown chawal": "brown rice",

    # ── Steam / Raw / Parboiled Rice ──────────────────────────────────────────
    "steam rice": "steam rice", "steam chawal": "steam rice",
    "steamed rice": "steam rice",
    "raw rice": "raw rice", "kaccha chawal": "raw rice",
    "parboiled rice": "parboiled rice", "usna chawal": "parboiled rice",
    "sela rice": "parboiled rice", "sela chawal": "parboiled rice",

    # ── Jasmine Rice ──────────────────────────────────────────────────────────
    "jasmine rice": "jasmine rice", "jasmine chawal": "jasmine rice",

    # ── Minket / Minketa Rice ─────────────────────────────────────────────────
    "minket": "minket rice", "minket rice": "minket rice",
    "minket chawal": "minket rice", "minketa": "minket rice",
    "minketa rice": "minket rice", "mini keta": "minket rice",

    # ── Mingat Rice (distinct variety, not a typo of minket) ──────────────────
    "mingat": "mingat rice", "mingat rice": "mingat rice",
    "minget": "mingat rice", "minget rice": "mingat rice",
    "mingat chawal": "mingat rice", "minget chawal": "mingat rice",

    # ── Trade Rice ────────────────────────────────────────────────────────────
    "trade rice": "trade rice", "trade chawal": "trade rice",
    "tred rice": "trade rice", "traid rice": "trade rice",

    # ── Wada Kolam Rice ───────────────────────────────────────────────────────
    "wada kolam": "wada kolam rice", "vada kolam": "wada kolam rice",
    "wada kolam rice": "wada kolam rice", "vada kolam rice": "wada kolam rice",
    "wada kolam chawal": "wada kolam rice", "vada kolam chawal": "wada kolam rice",
    "sriram wada kolam": "wada kolam rice",
    "sriram wada kolam rice": "wada kolam rice",

    # ── Kali Moong Rice ───────────────────────────────────────────────────────
    "kali moong": "kali moong rice", "kaali moong": "kali moong rice",
    "kali moong rice": "kali moong rice", "kaali moong rice": "kali moong rice",
    "kali moong chawal": "kali moong rice", "kaali moong chawal": "kali moong rice",
    "kali mooch rice": "kali moong rice",    # common transliteration error
    "black moong rice": "kali moong rice",

    # ── Yellow Rice ───────────────────────────────────────────────────────────
    "yellow rice": "yellow rice", "yellow chawal": "yellow rice",
    "pila chawal": "yellow rice", "peela chawal": "yellow rice",

    # ── Premium / Short-grain / Long-grain ────────────────────────────────────
    "premium rice": "premium rice", "premium chawal": "premium rice",
    "short grain rice": "short grain rice",
}

# Word-level substitutions applied to each word in compound names that
# don't appear as an exact phrase in _PRODUCT_ALIASES.
_WORD_SUBS: dict[str, str] = {
    "chawal": "rice", "chaawal": "rice", "chavel": "rice", "chawl": "rice",
    "tel": "oil", "sarson": "mustard", "sarsoon": "mustard",
    "daal": "dal", "dhal": "dal",
    "aatta": "atta",
}


def _normalize_product_name(name: str) -> str:
    lower = name.lower().strip()
    # 1. Exact phrase lookup
    if lower in _PRODUCT_ALIASES:
        return _PRODUCT_ALIASES[lower]
    # 2. Word-level substitution for compound names ("basmati chawal" → "basmati rice")
    words = lower.split()
    if len(words) > 1:
        substituted = " ".join(_WORD_SUBS.get(w, w) for w in words)
        if substituted != lower:
            # Re-check aliases with substituted form
            if substituted in _PRODUCT_ALIASES:
                return _PRODUCT_ALIASES[substituted]
            return substituted
    return lower


# ── OpenAI tool definitions ───────────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_stock",
            "description": (
                "Get real-time stock level for a product from the shop's inventory. "
                "ALWAYS call this when user asks about stock, available quantity, "
                "kitna maal hai, stock check karo, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_name": {
                        "type": "string",
                        "description": "Product name (e.g. 'arhar dal', 'basmati rice', 'aata')",
                    }
                },
                "required": ["product_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_customer_balance",
            "description": (
                "Get a customer's current outstanding balance from the ledger. "
                "ALWAYS call this when user asks about pending, baaki, udhar, "
                "balance, kitna dena hai, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {
                        "type": "string",
                        "description": "Customer name as mentioned by the user",
                    }
                },
                "required": ["customer_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_price",
            "description": (
                "Get the best available price for a product from the shop database. "
                "Checks the inventory table first (last_sale_price, last_purchase_price), "
                "then falls back to past transactions. "
                "ALWAYS call this for EVERY product in a sale whose rate_per_unit is not yet known. "
                "Call it even when the user says 'mujhe nhi pata', 'db se fetch karo', "
                "'check karo', 'I don't remember the price', etc. "
                "RESULT HANDLING — MANDATORY:\n"
                "  found=true → use returned rate. Set price_source: 'inventory'. NEVER ask user for price.\n"
                "  found=false AND ambiguous=true → rate_per_unit: null, price_source: 'ambiguous'. "
                "Do NOT ask user in chat — they pick from the product dropdown in the UI.\n"
                "  found=false (no match) → rate_per_unit: null, price_source: 'not_found'. "
                "HARD RULE: do NOT ask user for price, do NOT mention edit screen, do NOT proceed. "
                "The BACKEND automatically shows Add to Inventory and Skip & Continue buttons. "
                "NEVER set clarification_needed to a not-found message. "
                "ALWAYS include the item in transactions[]."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_name": {
                        "type": "string",
                        "description": "Product name exactly as the user said it (e.g. 'daal', 'paneer', 'rice')",
                    }
                },
                "required": ["product_name"],
            },
        },
    },
]


# ── Tool executor ─────────────────────────────────────────────────────────────

async def execute_tool(
    tool_name: str,
    tool_args: dict,
    db: AsyncSession,
    user_id: int,
) -> str:
    """Execute a tool call and return the result as a JSON string."""
    try:
        if tool_name == "get_stock":
            result = await inventory_service.get_stock(db, user_id, tool_args["product_name"])
        elif tool_name == "get_customer_balance":
            result = await inventory_service.get_customer_balance(db, user_id, tool_args["customer_name"])
        elif tool_name == "get_recent_price":
            raw_name = tool_args["product_name"]
            normalized = _normalize_product_name(raw_name)
            result = await inventory_service.get_recent_price(db, user_id, normalized)
            # Add raw_name to result so the caller knows what the user said
            if isinstance(result, dict):
                result["raw_name"] = raw_name
                result["normalized_name"] = normalized
        else:
            result = {"error": f"Unknown tool: {tool_name}"}
    except Exception as exc:
        _logger.error("Tool %s failed: %s", tool_name, exc)
        result = {"error": str(exc)}

    return json.dumps(result, ensure_ascii=False)
