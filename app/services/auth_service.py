from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.otp_code import OTPCode
from app.models.user import User
from app.schemas.auth import (
    SendOTPRequest,
    SendOTPResponse,
    VerifyOTPRequest,
    VerifyOTPResponse,
)
from app.services.firebase_service import verify_firebase_id_token
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


# ---------------------------------------------------------------------------
# send_otp — kept for API compatibility; OTP delivery now handled by Firebase
# on the client side. This endpoint is no longer called by the mobile app.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# verify_otp — Firebase-based: validates the ID token issued by the client
# after Firebase SMS OTP verification, then issues our own JWT.
# ---------------------------------------------------------------------------

async def verify_otp(db: AsyncSession, payload: VerifyOTPRequest) -> VerifyOTPResponse:
    # 1. Verify the Firebase ID token; raises HTTP 401 on failure.
    claims = await verify_firebase_id_token(
        payload.firebase_id_token,
        settings.firebase_project_id,
    )

    # 2. Extract phone number from the token (authoritative — ignore client field).
    firebase_phone: str | None = claims.get("phone_number")
    if not firebase_phone:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Firebase token does not contain a verified phone number.",
        )

    # 3. Normalise and look up / create the user record.
    phone_number = normalize_phone_number(firebase_phone)
    user, is_new_user = await _get_or_create_user(db, "phone", phone_number)
    await db.commit()

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive.",
        )

    # 4. Reload user with business relationship to check profile completeness.
    user_with_business = await db.scalar(
        select(User)
        .where(User.id == user.id)
        .options(selectinload(User.business))
    )
    business = getattr(user_with_business, "business", None) if user_with_business else None
    has_business = business is not None

    # 5. Issue our own JWT.
    access_token = create_access_token(
        {
            "sub": str(user.id),
            "user_id": user.id,
            "channel": "phone",
        }
    )

    return VerifyOTPResponse(
        message="OTP verified successfully.",
        verified=True,
        access_token=access_token,
        token_type="bearer",
        user_id=user.id,
        is_new_user=is_new_user,
        has_business=has_business,
        business_id=business.id if business else None,
        business_name=business.name if business else None,
    )
