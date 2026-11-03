import uuid
from sqlalchemy import Column, String, Boolean, DateTime
from sqlalchemy.sql import func
from app.db.base import Base

class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String, index=True)
    is_active = Column(Boolean(), default=True)
    is_superuser = Column(Boolean(), default=False)
    
    # Profile fields
    phone = Column(String, nullable=True)
    avatar_url = Column(String, nullable=True)
    role = Column(String, default="member")
    github_username = Column(String, nullable=True)
    gitlab_username = Column(String, nullable=True)

    # API Key（三方接口调用，仅存储哈希）
    api_key_hash = Column(String(64), unique=True, index=True, nullable=True)
    api_key_prefix = Column(String(16), nullable=True)
    api_key_created_at = Column(DateTime(timezone=True), nullable=True)
    api_key_expires_at = Column(DateTime(timezone=True), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())






