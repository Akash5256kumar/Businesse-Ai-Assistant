from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from app.core.config import settings


def generate_otp(length: int = 6) -> str:
    digits = "".join(str(secrets.randbelow(10)) for _ in range(length))
    return digits.zfill(length)


def hash_otp(identifier: str, otp: str) -> str:
    payload = f"{settings.otp_secret_key}:{identifier}:{otp}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_otp_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=settings.otp_expiry_minutes)


def mask_destination(destination: str) -> str:
    if "@" in destination:
        local_part, domain = destination.split("@", 1)
        if len(local_part) <= 2:
            masked_local = local_part[0] + "*" * max(len(local_part) - 1, 0)
        else:
            masked_local = local_part[:2] + "*" * (len(local_part) - 2)
        return f"{masked_local}@{domain}"

    if len(destination) <= 4:
        return "*" * len(destination)

    return "*" * (len(destination) - 4) + destination[-4:]


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_phone_number(phone_number: str) -> str:
    cleaned = "".join(char for char in phone_number if char.isdigit() or char == "+")
    if cleaned.startswith("++"):
        cleaned = cleaned.lstrip("+")
    return cleaned
