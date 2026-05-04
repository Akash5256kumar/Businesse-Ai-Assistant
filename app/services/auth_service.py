from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.otp_code import OTPCode
from app.models.user import User
from app.schemas.auth import (
    SendOTPRequest,
    SendOTPResponse,
    VerifyOTPRequest,
    VerifyOTPResponse,
)
from app.services.jwt_service import create_access_token
from app.services.otp_service import (
    generate_otp,
    get_otp_expiry,
    hash_otp,
    mask_destination,
    normalize_email,
    normalize_phone_number,
)


def _resolve_destination(email: str | None, phone_number: str | None) -> tuple[str, str]:
    if email:
        return "email", normalize_email(email)

    if phone_number:
        return "phone", normalize_phone_number(phone_number)

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="A destination is required to send OTP.",
    )


def _active_otp_query(channel: str, destination: str, purpose: str) -> Select[tuple[OTPCode]]:
    now = datetime.now(UTC)
    return (
        select(OTPCode)
        .where(OTPCode.channel == channel)
        .where(OTPCode.destination == destination)
        .where(OTPCode.purpose == purpose)
        .where(OTPCode.consumed_at.is_(None))
        .where(OTPCode.verified_at.is_(None))
        .where(OTPCode.expires_at > now)
        .order_by(OTPCode.created_at.desc())
    )


async def _get_or_create_user(
    db: AsyncSession,
    channel: str,
    destination: str,
) -> tuple[User, bool]:
    if channel == "email":
        query = select(User).where(User.email == destination)
        user = await db.scalar(query)
        if user is not None:
            return user, False

        user = User(email=destination)
    else:
        query = select(User).where(User.phone_number == destination)
        user = await db.scalar(query)
        if user is not None:
            return user, False

        user = User(phone_number=destination)

    db.add(user)
    await db.flush()
    return user, True


async def send_otp(db: AsyncSession, payload: SendOTPRequest) -> SendOTPResponse:
    channel, destination = _resolve_destination(payload.email, payload.phone_number)
    otp = generate_otp()
    expires_at = get_otp_expiry()

    await db.execute(
        update(OTPCode)
        .where(OTPCode.channel == channel)
        .where(OTPCode.destination == destination)
        .where(OTPCode.purpose == payload.purpose)
        .where(OTPCode.consumed_at.is_(None))
        .values(consumed_at=datetime.now(UTC))
    )

    otp_record = OTPCode(
        channel=channel,
        destination=destination,
        purpose=payload.purpose,
        code_hash=hash_otp(destination, otp),
        expires_at=expires_at,
    )
    db.add(otp_record)
    await db.commit()

    return SendOTPResponse(
        message="OTP generated successfully.",
        destination=mask_destination(destination),
        expires_in_seconds=settings.otp_expiry_minutes * 60,
        debug_otp=otp if settings.expose_test_otp else None,
    )


async def verify_otp(db: AsyncSession, payload: VerifyOTPRequest) -> VerifyOTPResponse:
    channel, destination = _resolve_destination(payload.email, payload.phone_number)
    otp_record = await db.scalar(_active_otp_query(channel, destination, payload.purpose))

    if otp_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active OTP found for this destination.",
        )

    provided_hash = hash_otp(destination, payload.otp)
    now = datetime.now(UTC)

    if otp_record.code_hash != provided_hash:
        otp_record.attempts += 1
        if otp_record.attempts >= settings.otp_max_attempts:
            otp_record.consumed_at = now
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OTP.",
        )

    user, is_new_user = await _get_or_create_user(db, channel, destination)
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive.",
        )

    otp_record.verified_at = now
    otp_record.consumed_at = now
    await db.commit()

    access_token = create_access_token(
        {
            "sub": str(user.id),
            "user_id": user.id,
            "channel": channel,
        }
    )

    return VerifyOTPResponse(
        message="OTP verified successfully.",
        verified=True,
        access_token=access_token,
        token_type="bearer",
        user_id=user.id,
        is_new_user=is_new_user,
    )
