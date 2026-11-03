from datetime import datetime, timedelta, timezone
from typing import Any, Tuple, Union
import hashlib
import secrets

from jose import jwt
from passlib.context import CryptContext
from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ALGORITHM = settings.ALGORITHM

API_KEY_PREFIX = "da_"

def create_access_token(
    subject: Union[str, Any], expires_delta: timedelta = None
) -> str:
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
    to_encode = {"exp": expire, "sub": str(subject)}
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


# ============ API Key（三方接口调用） ============

def generate_api_key() -> Tuple[str, str, str]:
    """
    生成 API Key

    返回 (raw_key, key_hash, key_prefix)：
    - raw_key: 完整 Key，仅在生成时返回一次，服务端只存哈希
    - key_hash: SHA-256 哈希（hex，64 位），存库用于校验
    - key_prefix: 展示用前缀（如 da_ab12cd34）
    """
    raw_key = f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    return raw_key, hash_api_key(raw_key), raw_key[:10]

def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()






