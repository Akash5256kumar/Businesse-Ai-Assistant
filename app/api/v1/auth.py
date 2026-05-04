from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.auth import (
    SendOTPRequest,
    SendOTPResponse,
    VerifyOTPRequest,
    VerifyOTPResponse,
)
from app.services.auth_service import send_otp, verify_otp

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/send-otp/", response_model=SendOTPResponse)
async def send_otp_endpoint(
    payload: SendOTPRequest,
    db: AsyncSession = Depends(get_db),
) -> SendOTPResponse:
    return await send_otp(db, payload)


@router.post("/verify-otp/", response_model=VerifyOTPResponse)
async def verify_otp_endpoint(
    payload: VerifyOTPRequest,
    db: AsyncSession = Depends(get_db),
) -> VerifyOTPResponse:
    return await verify_otp(db, payload)
