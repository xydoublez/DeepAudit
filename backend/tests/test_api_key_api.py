"""
Tests for app/api/v1/endpoints/users.py — API Key 管理端点

重点验证：生成 API Key 不再需要密码再确认，登录会话（含无本地密码的 SSO 用户）
认证通过即可直接生成/重新生成/吊销。
Uses direct function calls with mocked db sessions.
"""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints.users import (
    create_my_api_key,
    get_my_api_key,
    revoke_my_api_key,
)
from app.core import security
from app.models.user import User
from app.schemas.user import ApiKeyCreateRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(
    email="lizhiqiang@msunhealth.com",
    # SSO 用户无本地密码，库中为随机不可用哈希
    hashed_password="unusable-random-sso-hash",
):
    """Create a User ORM object (not persisted)."""
    user = User()
    user.email = email
    user.hashed_password = hashed_password
    user.full_name = "SSO User"
    user.is_active = True
    user.is_superuser = False
    user.role = "member"
    user.api_key_hash = None
    user.api_key_prefix = None
    user.api_key_created_at = None
    user.api_key_expires_at = None
    return user


def _mock_db():
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


# ===================================================================
# Schema
# ===================================================================

def test_request_schema_does_not_require_password():
    """请求体不再包含 password 字段，空 body 即可通过校验。"""
    assert "password" not in ApiKeyCreateRequest.model_fields
    req = ApiKeyCreateRequest.model_validate({})
    assert req.expires_days is None


# ===================================================================
# POST /users/me/api-key
# ===================================================================

@pytest.mark.asyncio
async def test_sso_user_generates_key_without_password():
    """SSO 用户（无本地密码）无需密码确认即可直接生成 API Key。"""
    user = _make_user()
    db = _mock_db()

    resp = await create_my_api_key(
        db=db, request=ApiKeyCreateRequest(), current_user=user
    )

    assert resp.api_key.startswith("da_")
    assert resp.api_key_prefix == resp.api_key[:10]
    # 服务端只存哈希
    assert user.api_key_hash == security.hash_api_key(resp.api_key)
    assert user.api_key_prefix == resp.api_key_prefix
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_expiry_is_one_year():
    user = _make_user()
    resp = await create_my_api_key(
        db=_mock_db(), request=ApiKeyCreateRequest(), current_user=user
    )
    assert resp.api_key_expires_at - resp.api_key_created_at == timedelta(days=365)


@pytest.mark.asyncio
async def test_custom_expiry_days():
    user = _make_user()
    resp = await create_my_api_key(
        db=_mock_db(), request=ApiKeyCreateRequest(expires_days=30), current_user=user
    )
    assert resp.api_key_expires_at - resp.api_key_created_at == timedelta(days=30)


@pytest.mark.asyncio
async def test_invalid_expiry_days_rejected():
    user = _make_user()
    with pytest.raises(HTTPException) as exc_info:
        await create_my_api_key(
            db=_mock_db(), request=ApiKeyCreateRequest(expires_days=0), current_user=user
        )
    assert exc_info.value.status_code == 400

    with pytest.raises(HTTPException) as exc_info:
        await create_my_api_key(
            db=_mock_db(),
            request=ApiKeyCreateRequest(expires_days=3651),
            current_user=user,
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_regenerate_invalidates_old_key():
    """重新生成后旧 Key 立即失效：哈希被替换为新 Key 的哈希。"""
    user = _make_user()
    db = _mock_db()

    first = await create_my_api_key(
        db=db, request=ApiKeyCreateRequest(), current_user=user
    )
    old_hash = user.api_key_hash

    second = await create_my_api_key(
        db=db, request=ApiKeyCreateRequest(), current_user=user
    )

    assert second.api_key != first.api_key
    assert user.api_key_hash != old_hash
    assert user.api_key_hash == security.hash_api_key(second.api_key)
    assert security.hash_api_key(first.api_key) != user.api_key_hash


# ===================================================================
# GET / DELETE /users/me/api-key
# ===================================================================

@pytest.mark.asyncio
async def test_get_and_revoke_api_key():
    user = _make_user()
    db = _mock_db()

    await create_my_api_key(db=db, request=ApiKeyCreateRequest(), current_user=user)
    info = await get_my_api_key(current_user=user)
    assert info.has_api_key is True
    assert info.is_expired is False
    assert info.api_key_prefix == user.api_key_prefix

    revoked = await revoke_my_api_key(db=db, current_user=user)
    assert revoked.has_api_key is False
    assert user.api_key_hash is None
    assert user.api_key_prefix is None
    assert user.api_key_created_at is None
    assert user.api_key_expires_at is None
