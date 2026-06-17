from __future__ import annotations

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.auth import (
    ProfileSetupRequest,
    ProfileSetupResponse,
    PushTokenRegisterRequest,
    PushTokenRegisterResponse,
)
from app.services.profile_service import register_push_token, setup_profile


class BusinessProfileResponse(BaseModel):
    owner_name: str
    phone: str | None = None
    email: str | None = None
    business_name: str | None = None
    business_location: str | None = None
    shop_type: str | None = None
    whatsapp_reminders_enabled: bool = True


router = APIRouter(prefix="/api/v1/profile", tags=["profile"])


@router.post(
    "/setup",
    response_model=ProfileSetupResponse,
    status_code=status.HTTP_200_OK,
)
async def setup_profile_endpoint(
    payload: ProfileSetupRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProfileSetupResponse:
    return await setup_profile(db, current_user, payload)


@router.get(
    "/me",
    response_model=BusinessProfileResponse,
    status_code=status.HTTP_200_OK,
)
async def get_profile_endpoint(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BusinessProfileResponse:
    result = await db.execute(
        select(User).where(User.id == current_user.id).options(selectinload(User.business))
    )
    user: User = result.scalar_one()
    business = user.business if user.user_type == "business" else None
    return BusinessProfileResponse(
        owner_name=user.full_name or "",
        phone=user.phone_number,
        email=user.email,
        business_name=business.name if business else None,
        business_location=business.location if business else None,
        shop_type=business.shop_type if business else None,
        whatsapp_reminders_enabled=business.whatsapp_reminders_enabled if business else True,
    )


@router.post(
    "/push-token",
    response_model=PushTokenRegisterResponse,
    status_code=status.HTTP_200_OK,
)
async def register_push_token_endpoint(
    payload: PushTokenRegisterRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PushTokenRegisterResponse:
    return await register_push_token(db, current_user, payload)
