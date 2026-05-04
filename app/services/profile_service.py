from __future__ import annotations

import re

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.business import Business
from app.models.user import User
from app.schemas.auth import ProfileSetupRequest, ProfileSetupResponse


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-") or "business"


async def _unique_slug(
    db: AsyncSession, base_slug: str, exclude_id: int | None = None
) -> str:
    counter = 0
    while True:
        candidate = base_slug if counter == 0 else f"{base_slug}-{counter}"
        q = select(Business).where(Business.slug == candidate)
        if exclude_id is not None:
            q = q.where(Business.id != exclude_id)
        if await db.scalar(q) is None:
            return candidate
        counter += 1


async def setup_profile(
    db: AsyncSession,
    user: User,
    payload: ProfileSetupRequest,
) -> ProfileSetupResponse:
    user.full_name = payload.full_name
    user.user_type = payload.user_type

    # ── Customer: no business required ──────────────────────────────────────
    if payload.user_type == "customer":
        await db.flush()
        return ProfileSetupResponse(
            message="Profile set up successfully.",
            user_id=user.id,
            full_name=user.full_name,
            user_type=user.user_type,
        )

    # ── Business user: business_name is required ─────────────────────────────
    if not payload.business_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="business_name is required for business users.",
        )

    existing = await db.scalar(
        select(Business).where(Business.owner_id == user.id)
    )

    if existing is not None:
        if existing.name != payload.business_name:
            base_slug = _slugify(payload.business_name)
            existing.slug = await _unique_slug(db, base_slug, exclude_id=existing.id)
            existing.name = payload.business_name
        existing.location = payload.location
        await db.flush()
        return ProfileSetupResponse(
            message="Profile updated successfully.",
            user_id=user.id,
            full_name=user.full_name,
            user_type=user.user_type,
            business_id=existing.id,
            business_name=existing.name,
            location=existing.location,
        )

    slug = await _unique_slug(db, _slugify(payload.business_name))
    business = Business(
        name=payload.business_name,
        slug=slug,
        location=payload.location,
        owner_id=user.id,
    )
    db.add(business)
    await db.flush()

    return ProfileSetupResponse(
        message="Profile set up successfully.",
        user_id=user.id,
        full_name=user.full_name,
        user_type=user.user_type,
        business_id=business.id,
        business_name=business.name,
        location=business.location,
    )
