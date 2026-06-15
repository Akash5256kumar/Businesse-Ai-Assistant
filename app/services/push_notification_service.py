from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.device_token import DeviceToken

try:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
except ImportError:  # pragma: no cover - graceful fallback when dependency is absent
    Request = None
    service_account = None

_logger = logging.getLogger(__name__)
_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_FCM_URL_TEMPLATE = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
_INVALID_TOKEN_CODES = {"UNREGISTERED", "INVALID_ARGUMENT"}


class PushNotificationService:
    def __init__(self) -> None:
        self._access_token: str | None = None
        self._access_token_expiry: datetime | None = None
        self._credentials = None

    def is_configured(self) -> bool:
        return bool(
            settings.firebase_project_id
            and (
                settings.firebase_credentials_json
                or (settings.firebase_client_email and settings.firebase_private_key)
            )
        )

    async def send_to_user(
        self,
        db: AsyncSession,
        user_id: int,
        *,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> int:
        if not self.is_configured():
            _logger.info("Skipping push notification because Firebase is not configured")
            return 0

        result = await db.execute(
            select(DeviceToken).where(
                DeviceToken.user_id == user_id,
                DeviceToken.is_active.is_(True),
            )
        )
        tokens = list(result.scalars().all())
        if not tokens:
            return 0

        access_token = await self._get_access_token()
        if not access_token:
            _logger.warning("Skipping push notification because access token could not be created")
            return 0

        sent_count = 0
        async with httpx.AsyncClient(timeout=20.0) as client:
            for device_token in tokens:
                was_sent = await self._send_to_token(
                    client,
                    db,
                    device_token,
                    access_token=access_token,
                    title=title,
                    body=body,
                    data=data or {},
                )
                if was_sent:
                    sent_count += 1
        return sent_count

    async def _send_to_token(
        self,
        client: httpx.AsyncClient,
        db: AsyncSession,
        device_token: DeviceToken,
        *,
        access_token: str,
        title: str,
        body: str,
        data: dict[str, str],
    ) -> bool:
        payload = {
            "message": {
                "token": device_token.token,
                "notification": {"title": title, "body": body},
                "data": data,
            }
        }
        try:
            response = await client.post(
                _FCM_URL_TEMPLATE.format(project_id=settings.firebase_project_id),
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=payload,
            )
        except httpx.HTTPError as exc:
            _logger.warning("FCM request failed for token %s: %s", device_token.id, exc)
            return False

        if response.is_success:
            device_token.last_seen_at = datetime.now(timezone.utc)
            return True

        invalid_code = self._extract_error_code(response)
        if invalid_code in _INVALID_TOKEN_CODES:
            device_token.is_active = False

        _logger.warning(
            "FCM send failed for token %s with status %s: %s",
            device_token.id,
            response.status_code,
            response.text,
        )
        await db.flush()
        return False

    async def _get_access_token(self) -> str | None:
        if self._access_token and self._access_token_expiry:
            if self._access_token_expiry - timedelta(minutes=5) > datetime.now(timezone.utc):
                return self._access_token
        return await asyncio.to_thread(self._refresh_access_token_sync)

    def _refresh_access_token_sync(self) -> str | None:
        if Request is None or service_account is None:
            _logger.warning("google-auth is not installed; Firebase push notifications are disabled")
            return None

        try:
            if self._credentials is None:
                self._credentials = service_account.Credentials.from_service_account_info(
                    self._build_service_account_info(),
                    scopes=[_FCM_SCOPE],
                )
            self._credentials.refresh(Request())
        except Exception as exc:  # pragma: no cover - depends on env credentials
            _logger.warning("Failed to refresh Firebase access token: %s", exc)
            return None

        self._access_token = self._credentials.token
        self._access_token_expiry = self._credentials.expiry
        return self._access_token

    def _build_service_account_info(self) -> dict[str, Any]:
        if settings.firebase_credentials_json:
            return json.loads(settings.firebase_credentials_json)

        private_key = settings.firebase_private_key.replace("\\n", "\n")
        return {
            "type": "service_account",
            "project_id": settings.firebase_project_id,
            "private_key": private_key,
            "client_email": settings.firebase_client_email,
            "token_uri": "https://oauth2.googleapis.com/token",
        }

    @staticmethod
    def _extract_error_code(response: httpx.Response) -> str | None:
        try:
            body = response.json()
        except ValueError:
            return None

        details = body.get("error", {}).get("details", [])
        for detail in details:
            code = detail.get("errorCode")
            if code:
                return str(code)

        status = body.get("error", {}).get("status")
        if status:
            return str(status)
        return None


push_notification_service = PushNotificationService()
