from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.auth import ProfileSetupRequest, ProfileSetupResponse
from app.services.profile_service import setup_profile

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
