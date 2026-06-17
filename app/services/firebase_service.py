from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException, status

_logger = logging.getLogger(__name__)

# Public-key URL used by google.oauth2.id_token internally — listed here only
# so the import path is clear to readers.
_FIREBASE_CERT_URL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"


async def verify_firebase_id_token(
    id_token: str,
    project_id: str,
) -> dict[str, Any]:
    """Verify a Firebase Authentication ID token and return the decoded claims.

    Uses ``google.oauth2.id_token.verify_firebase_token`` which fetches
    Google's public signing certificates over HTTPS (cached per process).

    Args:
        id_token:   The raw Firebase ID token sent by the mobile client.
        project_id: Firebase project ID (used as the ``aud`` claim check).

    Returns:
        Decoded JWT payload, e.g. ``{"phone_number": "+919876543210", "sub": "...", ...}``.

    Raises:
        HTTPException 500 if ``google-auth`` is not installed.
        HTTPException 401 if the token is invalid, expired, or has wrong audience.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.id_token import verify_firebase_token
    except ImportError:
        _logger.error("google-auth is not installed; cannot verify Firebase tokens")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Firebase verification is unavailable.",
        )

    if not project_id:
        _logger.error("FIREBASE_PROJECT_ID is not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server is not configured for Firebase authentication.",
        )

    def _verify_sync() -> dict[str, Any]:
        return verify_firebase_token(id_token, Request(), audience=project_id)

    try:
        return await asyncio.to_thread(_verify_sync)
    except Exception as exc:
        _logger.warning("Firebase token verification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firebase authentication token is invalid or has expired.",
        )
