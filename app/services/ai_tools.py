from __future__ import annotations

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import inventory_service

_logger = logging.getLogger(__name__)

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
                "'check karo', 'I don't remember the price', etc. — that is an explicit instruction "
                "to look up the price from the database. "
                "If found=true → use the returned rate directly. NEVER ask user for price. "
                "If found=false AND ambiguous=true → keep rate_per_unit: null, price_source: 'user'. "
                "Do NOT ask user in chat — they pick from the product dropdown in the UI. "
                "If found=false (no match) → keep rate_per_unit: null. NEVER ask user for rate."
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
            result = await inventory_service.get_recent_price(db, user_id, tool_args["product_name"])
        else:
            result = {"error": f"Unknown tool: {tool_name}"}
    except Exception as exc:
        _logger.error("Tool %s failed: %s", tool_name, exc)
        result = {"error": str(exc)}

    return json.dumps(result, ensure_ascii=False)
