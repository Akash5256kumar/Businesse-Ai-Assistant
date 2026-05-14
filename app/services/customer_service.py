from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer

_HONORIFICS = re.compile(
    r"\s*(ji|bhai|bhaiya|sahab|sahib|sir|madam|ben|didi)\s*$",
    re.IGNORECASE,
)


def _clean_name(name: str) -> str:
    return _HONORIFICS.sub("", name.strip()).strip()


# ── Basic CRUD ────────────────────────────────────────────────────────────────

async def get_or_create(db: AsyncSession, user_id: int, name: str) -> Customer:
    normalised = name.strip()
    result = await db.execute(
        select(Customer).where(
            Customer.user_id == user_id,
            Customer.name.ilike(normalised),
        )
    )
    customer = result.scalar_one_or_none()

    if customer is None:
        customer = Customer(user_id=user_id, name=normalised)
        db.add(customer)
        await db.flush()

    return customer


async def get_by_id(db: AsyncSession, customer_id: int) -> Customer | None:
    return await db.get(Customer, customer_id)


async def get_by_phone(db: AsyncSession, user_id: int, phone: str) -> Customer | None:
    result = await db.execute(
        select(Customer).where(Customer.user_id == user_id, Customer.phone == phone.strip())
    )
    return result.scalar_one_or_none()


async def create_customer(
    db: AsyncSession, user_id: int, name: str, phone: str | None = None
) -> Customer:
    customer = Customer(user_id=user_id, name=name.strip(), phone=phone)
    db.add(customer)
    await db.flush()
    return customer


# ── Search ────────────────────────────────────────────────────────────────────

async def search_by_name(db: AsyncSession, user_id: int, name: str) -> list[Customer]:
    """Case-insensitive partial match. Up to 5 results. No MuRIL ranking."""
    clean = _clean_name(name)
    result = await db.execute(
        select(Customer)
        .where(Customer.user_id == user_id, Customer.name.ilike(f"%{clean}%"))
        .order_by(Customer.name)
        .limit(5)
    )
    return list(result.scalars().all())


async def search_by_name_with_muril(
    db: AsyncSession,
    user_id: int,
    name: str,
    top_k: int = 5,
) -> list[tuple[Customer, float]]:
    """
    Enhanced customer search combining ilike and MuRIL embedding similarity.

    Strategy:
      1. Broad ilike search (fast, catches same-script names)
      2. If zero results and MuRIL is available: full embedding scan over up to
         200 stored names — catches cross-script variations (Raju / राजू / Rajoo)
      3. All candidates ranked by MuRIL cosine similarity; top_k returned with scores.
      4. Graceful fallback to plain ilike when MuRIL is not installed.

    Returns list of (Customer, similarity_score) sorted descending.
    similarity_score == 0.0 when MuRIL was not used.
    """
    from app.core.config import settings
    from app.services.muril_service import muril_service

    clean = _clean_name(name)

    # ── Step 1: broad ilike ───────────────────────────────────────────────────
    ilike_res = await db.execute(
        select(Customer)
        .where(Customer.user_id == user_id, Customer.name.ilike(f"%{clean}%"))
        .order_by(Customer.name)
        .limit(20)  # wider than usual so MuRIL can re-rank
    )
    candidates: list[Customer] = list(ilike_res.scalars().all())

    # ── Step 2: embedding scan when ilike finds nothing ───────────────────────
    if not candidates and muril_service.is_available():
        all_res = await db.execute(
            select(Customer)
            .where(Customer.user_id == user_id)
            .limit(200)
        )
        candidates = list(all_res.scalars().all())

    if not candidates:
        return []

    # ── Step 3: MuRIL ranking ─────────────────────────────────────────────────
    if muril_service.is_available():
        candidate_names = [c.name for c in candidates]
        scores = await muril_service.compute_name_similarities(name, candidate_names)
        paired = sorted(zip(candidates, scores), key=lambda x: x[1], reverse=True)

        threshold = settings.muril_similarity_threshold
        filtered = [(c, s) for c, s in paired if s >= threshold][:top_k]

        # If everything is below threshold but we have ilike results, return them
        # with score 0 rather than showing an empty list.
        if not filtered and candidates[:top_k]:
            return [(c, 0.0) for c in candidates[:top_k]]

        return filtered

    # ── Fallback: return ilike results with score 0.0 ─────────────────────────
    return [(c, 0.0) for c in candidates[:top_k]]
