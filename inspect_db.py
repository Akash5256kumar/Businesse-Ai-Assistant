"""
Quick database inspection script.
Run: python inspect_db.py
"""
import asyncio
import os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/apna_business",
)

QUERIES = {
    "Users": "SELECT id, full_name, phone, user_type, created_at FROM users ORDER BY id DESC LIMIT 20",
    "Transactions": "SELECT id, user_id, type, amount, is_credit, created_at FROM transactions ORDER BY id DESC LIMIT 20",
    "Customers": "SELECT id, user_id, name, phone, pending, created_at FROM customers ORDER BY id DESC LIMIT 20",
    "Device Tokens": "SELECT id, user_id, platform, is_active, last_seen_at FROM device_tokens ORDER BY id DESC LIMIT 20",
    "Notifications": "SELECT id, user_id, title, body, is_read, sent_at FROM notification_logs ORDER BY id DESC LIMIT 20",
    "Reminder Logs": "SELECT id, user_id, customer_id, channel, status, sent_at FROM reminder_logs ORDER BY id DESC LIMIT 10",
}


async def inspect():
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.connect() as conn:
        for table_name, query in QUERIES.items():
            try:
                result = await conn.execute(text(query))
                rows = result.fetchall()
                cols = result.keys()
                print(f"\n{'='*60}")
                print(f"  {table_name}  ({len(rows)} rows shown)")
                print(f"{'='*60}")
                if not rows:
                    print("  (empty)")
                    continue
                col_list = list(cols)
                header = " | ".join(f"{c:<20}" for c in col_list)
                print(header)
                print("-" * len(header))
                for row in rows:
                    line = " | ".join(f"{str(v):<20}" for v in row)
                    print(line)
            except Exception as e:
                print(f"\n  {table_name}: ERROR — {e}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(inspect())
