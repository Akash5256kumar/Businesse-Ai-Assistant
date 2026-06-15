from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

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
