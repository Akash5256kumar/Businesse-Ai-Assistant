"""
Tests for POST /api/v1/profile/setup

Coverage:
  Happy path
    - new user (no business) → creates profile
    - existing user (has business) → updates profile
    - business name unchanged → no slug re-generation

  Input validation (422)
    - missing full_name
    - missing business_name
    - full_name is empty string
    - business_name is empty string
    - full_name is whitespace-only
    - business_name is whitespace-only
    - full_name too short (1 char)
    - business_name too short (1 char)
    - full_name too long (101 chars)
    - business_name too long (101 chars)

  Authentication & authorisation
    - no Authorization header  → 403
    - malformed Bearer token   → 401
    - expired JWT              → 401
    - valid JWT, user missing  → 401
    - valid JWT, user inactive → 401
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient
from jose import jwt

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.main import app
from app.models.business import Business
from app.models.user import User

URL = "/api/v1/profile/setup"

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def _make_token(user_id: int, expired: bool = False) -> str:
    delta = timedelta(seconds=-10) if expired else timedelta(hours=1)
    payload = {"sub": str(user_id), "exp": datetime.now(UTC) + delta}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mock_db(
    existing_business: Business | None = None,
    new_business_id: int = 99,
) -> AsyncMock:
    """Return a mock AsyncSession wired for profile_service queries."""
    db = AsyncMock()
    # db.scalar: first call checks for existing business, second checks slug uniqueness
    if existing_business is not None:
        db.scalar.side_effect = [existing_business, None]
    else:
        db.scalar.return_value = None

    added_objects: list = []
    db.add = MagicMock(side_effect=added_objects.append)

    async def _flush():
        # Simulate SQLAlchemy populating the PK after a real flush
        for obj in added_objects:
            if isinstance(obj, Business) and obj.id is None:
                obj.id = new_business_id

    db.flush = AsyncMock(side_effect=_flush)
    return db


def _mock_user(user_id: int = 1, active: bool = True) -> User:
    user = MagicMock(spec=User)
    user.id = user_id
    user.is_active = active
    user.full_name = None
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


# ──────────────────────────────────────────────
# Happy-path tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_setup_profile_creates_new_business(client: AsyncClient):
    user = _mock_user()
    db = _mock_db(existing_business=None)
    _override_auth(user)
    _override_db(db)
    try:
        resp = await client.post(URL, json={"full_name": "Alice Sharma", "business_name": "Alice Shop"})
    finally:
        _clear_overrides()

    assert resp.status_code == 200
    body = resp.json()
    assert body["message"] == "Profile set up successfully."
    assert body["full_name"] == "Alice Sharma"
    assert body["business_name"] == "Alice Shop"
    assert body["user_id"] == 1
    db.add.assert_called_once()
    db.flush.assert_called_once()


@pytest.mark.asyncio
async def test_setup_profile_updates_existing_business(client: AsyncClient):
    user = _mock_user()
    existing = MagicMock(spec=Business)
    existing.id = 5
    existing.name = "Old Shop"
    existing.slug = "old-shop"
    db = _mock_db(existing_business=existing)
    _override_auth(user)
    _override_db(db)
    try:
        resp = await client.post(URL, json={"full_name": "Bob Gupta", "business_name": "New Shop"})
    finally:
        _clear_overrides()

    assert resp.status_code == 200
    body = resp.json()
    assert body["message"] == "Profile updated successfully."
    assert body["full_name"] == "Bob Gupta"
    assert body["business_name"] == "New Shop"
    db.flush.assert_called_once()


@pytest.mark.asyncio
async def test_setup_profile_same_business_name_skips_slug_update(client: AsyncClient):
    user = _mock_user()
    existing = MagicMock(spec=Business)
    existing.id = 5
    existing.name = "Same Shop"
    existing.slug = "same-shop"
    # Only one scalar call expected (check existing business; no slug re-check)
    db = AsyncMock()
    db.scalar.return_value = existing
    db.flush = AsyncMock()
    db.add = MagicMock()
    _override_auth(user)
    _override_db(db)
    try:
        resp = await client.post(URL, json={"full_name": "Carol", "business_name": "Same Shop"})
    finally:
        _clear_overrides()

    assert resp.status_code == 200
    assert resp.json()["message"] == "Profile updated successfully."
    # slug re-generation query not issued
    assert db.scalar.call_count == 1


# ──────────────────────────────────────────────
# Input validation tests (422)
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_full_name(client: AsyncClient):
    _override_auth(_mock_user())
    _override_db(_mock_db())
    try:
        resp = await client.post(URL, json={"business_name": "My Shop"})
    finally:
        _clear_overrides()
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_missing_business_name(client: AsyncClient):
    _override_auth(_mock_user())
    _override_db(_mock_db())
    try:
        resp = await client.post(URL, json={"full_name": "Alice"})
    finally:
        _clear_overrides()
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_empty_full_name(client: AsyncClient):
    _override_auth(_mock_user())
    _override_db(_mock_db())
    try:
        resp = await client.post(URL, json={"full_name": "", "business_name": "My Shop"})
    finally:
        _clear_overrides()
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_empty_business_name(client: AsyncClient):
    _override_auth(_mock_user())
    _override_db(_mock_db())
    try:
        resp = await client.post(URL, json={"full_name": "Alice", "business_name": ""})
    finally:
        _clear_overrides()
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_whitespace_only_full_name(client: AsyncClient):
    _override_auth(_mock_user())
    _override_db(_mock_db())
    try:
        resp = await client.post(URL, json={"full_name": "   ", "business_name": "My Shop"})
    finally:
        _clear_overrides()
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_whitespace_only_business_name(client: AsyncClient):
    _override_auth(_mock_user())
    _override_db(_mock_db())
    try:
        resp = await client.post(URL, json={"full_name": "Alice", "business_name": "   "})
    finally:
        _clear_overrides()
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_full_name_too_short(client: AsyncClient):
    _override_auth(_mock_user())
    _override_db(_mock_db())
    try:
        resp = await client.post(URL, json={"full_name": "A", "business_name": "My Shop"})
    finally:
        _clear_overrides()
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_business_name_too_short(client: AsyncClient):
    _override_auth(_mock_user())
    _override_db(_mock_db())
    try:
        resp = await client.post(URL, json={"full_name": "Alice", "business_name": "X"})
    finally:
        _clear_overrides()
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_full_name_too_long(client: AsyncClient):
    _override_auth(_mock_user())
    _override_db(_mock_db())
    try:
        resp = await client.post(URL, json={"full_name": "A" * 101, "business_name": "My Shop"})
    finally:
        _clear_overrides()
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_business_name_too_long(client: AsyncClient):
    _override_auth(_mock_user())
    _override_db(_mock_db())
    try:
        resp = await client.post(URL, json={"full_name": "Alice", "business_name": "B" * 101})
    finally:
        _clear_overrides()
    assert resp.status_code == 422


# ──────────────────────────────────────────────
# Authentication / authorisation tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_auth_header_returns_401(client: AsyncClient):
    resp = await client.post(URL, json={"full_name": "Alice", "business_name": "My Shop"})
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_malformed_token_returns_401(client: AsyncClient):
    resp = await client.post(
        URL,
        json={"full_name": "Alice", "business_name": "My Shop"},
        headers=_auth("this.is.not.a.jwt"),
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_expired_token_returns_401(client: AsyncClient):
    token = _make_token(user_id=1, expired=True)
    # DB override not needed: jose raises JWTError before any DB call
    resp = await client.post(
        URL,
        json={"full_name": "Alice", "business_name": "My Shop"},
        headers=_auth(token),
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_valid_token_user_not_found_returns_401(client: AsyncClient):
    token = _make_token(user_id=999)
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    try:
        resp = await client.post(
            URL,
            json={"full_name": "Alice", "business_name": "My Shop"},
            headers=_auth(token),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_valid_token_inactive_user_returns_401(client: AsyncClient):
    token = _make_token(user_id=2)
    inactive = _mock_user(user_id=2, active=False)
    db = AsyncMock()
    db.get = AsyncMock(return_value=inactive)

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    try:
        resp = await client.post(
            URL,
            json={"full_name": "Alice", "business_name": "My Shop"},
            headers=_auth(token),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 401
