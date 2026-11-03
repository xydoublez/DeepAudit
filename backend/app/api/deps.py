from datetime import datetime, timezone
from typing import Generator, Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer
from jose import jwt, JWTError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.core import security
from app.core.config import settings
from app.db.session import get_db
from app.models.user import User
from app.schemas import token as token_schema

reusable_oauth2 = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/auth/login", auto_error=False
)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def _get_user_by_api_key(db: AsyncSession, api_key: str) -> User:
    """通过 X-API-Key 认证：哈希查库 + 有效性/过期校验"""
    key_hash = security.hash_api_key(api_key)
    result = await db.execute(select(User).where(User.api_key_hash == key_hash))
    user = result.scalars().first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的 API Key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    if not user.is_active:
        raise HTTPException(status_code=400, detail="用户已被禁用")
    if user.api_key_expires_at:
        expires_at = user.api_key_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API Key 已过期，请重新生成",
                headers={"WWW-Authenticate": "ApiKey"},
            )
    return user

async def _get_user_by_jwt(db: AsyncSession, token: str) -> User:
    """JWT Bearer 认证（Web 登录会话）"""
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[security.ALGORITHM]
        )
        token_data = token_schema.TokenPayload(**payload)
    except (JWTError, ValidationError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无法验证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(select(User).where(User.id == token_data.sub))
    user = result.scalars().first()

    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not user.is_active:
        raise HTTPException(status_code=400, detail="用户已被禁用")
    return user

async def get_current_user(
    db: AsyncSession = Depends(get_db),
    token: Optional[str] = Depends(reusable_oauth2),
    api_key: Optional[str] = Depends(api_key_header),
) -> User:
    # 优先 API Key（三方接口调用），其次 JWT Bearer（Web 登录）
    if api_key:
        return await _get_user_by_api_key(db, api_key)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await _get_user_by_jwt(db, token)

async def get_current_user_jwt_only(
    db: AsyncSession = Depends(get_db),
    token: Optional[str] = Depends(reusable_oauth2),
    api_key: Optional[str] = Depends(api_key_header),
) -> User:
    """敏感操作专用：仅接受 JWT 登录会话，拒绝 API Key

    用于 API Key 管理、密码修改等凭据生命周期端点，
    防止 API Key 自我续期或短时凭据兑换长效凭据。
    """
    if api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="此操作不允许使用 API Key，请使用登录会话（Bearer Token）",
        )
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await _get_user_by_jwt(db, token)

async def get_current_active_superuser(
    current_user: User = Depends(get_current_user),
) -> User:
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=400, detail="权限不足"
        )
    return current_user
