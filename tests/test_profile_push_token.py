from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from app.core.auth import get_current_user
from app.core.database import get_db
from app.main import app
from app.models.device_token import DeviceToken
from app.models.user import User

URL = "/api/v1/profile/push-token"


def _mock_user(user_id: int = 1) -> User:
    user = MagicMock(spec=User)
    user.id = user_id
    user.is_active = True
    return user


def _override_auth(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _override_db(db: AsyncMock):
    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db


def _clear_overrides():
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


def _mock_db_for_new_token(new_token_id: int = 77) -> AsyncMock:
    db = AsyncMock()
    db.scalar.return_value = None

    added_objects: list = []
    db.add = MagicMock(side_effect=added_objects.append)

    async def _flush():
        for obj in added_objects:
            if isinstance(obj, DeviceToken) and obj.id is None:
                obj.id = new_token_id

    db.flush = AsyncMock(side_effect=_flush)
    return db


@pytest.mark.asyncio
async def test_register_push_token_creates_token(client: AsyncClient):
    _override_auth(_mock_user())
    db = _mock_db_for_new_token()
    _override_db(db)
    try:
        resp = await client.post(
            URL,
            json={
                "token": "fcm_token_value_that_is_long_enough_1234567890",
                "platform": "android",
                "device_id": "pixel-01",
                "app_version": "1.0.0",
            },
        )
    finally:
        _clear_overrides()

    assert resp.status_code == 200
    body = resp.json()
    assert body["message"] == "Push token registered successfully."
    assert body["token_id"] == 77
    assert body["is_active"] is True
    db.add.assert_called_once()
    db.flush.assert_called_once()


@pytest.mark.asyncio
async def test_register_push_token_updates_existing_token(client: AsyncClient):
    _override_auth(_mock_user(user_id=9))
    existing = DeviceToken(
        id=12,
        user_id=1,
        token="existing_fcm_token_value_that_is_long_enough_1234567890",
        platform="android",
        device_id="old-device",
        app_version="0.9.0",
        is_active=False,
    )
    db = AsyncMock()
    db.scalar.return_value = existing
    db.flush = AsyncMock()
    db.add = MagicMock()
    _override_db(db)
    try:
        resp = await client.post(
            URL,
            json={
                "token": "existing_fcm_token_value_that_is_long_enough_1234567890",
                "platform": "ios",
                "device_id": "iphone-15",
                "app_version": "1.1.0",
            },
        )
    finally:
        _clear_overrides()

    assert resp.status_code == 200
    body = resp.json()
    assert body["message"] == "Push token updated successfully."
    assert body["token_id"] == 12
    assert existing.user_id == 9
    assert existing.platform == "ios"
    assert existing.device_id == "iphone-15"
    assert existing.app_version == "1.1.0"
    assert existing.is_active is True
    db.add.assert_not_called()
    db.flush.assert_called_once()


@pytest.mark.asyncio
async def test_register_push_token_rejects_short_token(client: AsyncClient):
    _override_auth(_mock_user())
    _override_db(_mock_db_for_new_token())
    try:
        resp = await client.post(
            URL,
            json={"token": "too-short", "platform": "android"},
        )
    finally:
        _clear_overrides()

    assert resp.status_code == 422
