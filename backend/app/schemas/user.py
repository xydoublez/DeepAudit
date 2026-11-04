from typing import Optional, List
from pydantic import BaseModel, EmailStr

class UserBase(BaseModel):
    email: Optional[EmailStr] = None
    is_active: Optional[bool] = True
    is_superuser: bool = False
    full_name: Optional[str] = None
    
    # Profile fields
    phone: Optional[str] = None
    avatar_url: Optional[str] = None
    role: str = "member"
    github_username: Optional[str] = None
    gitlab_username: Optional[str] = None

class UserCreate(UserBase):
    email: EmailStr
    password: str
    full_name: str

class UserUpdate(UserBase):
    password: Optional[str] = None

class UserInDBBase(UserBase):
    id: str
    created_at: Optional[object] = None # Datetime
    updated_at: Optional[object] = None

    class Config:
        from_attributes = True

class User(UserInDBBase):
    pass

class UserListResponse(BaseModel):
    users: List[User]
    total: int
    skip: int
    limit: int


class ApiKeyInfo(BaseModel):
    """API Key 状态信息（不含 Key 本体）"""
    has_api_key: bool
    api_key_prefix: Optional[str] = None
    api_key_created_at: Optional[object] = None
    api_key_expires_at: Optional[object] = None
    is_expired: bool = False


class ApiKeyCreateRequest(BaseModel):
    # 登录会话（JWT/SSO）即为授权依据，无需密码再确认
    expires_days: Optional[int] = None  # 有效期（天），默认取配置 API_KEY_EXPIRE_DAYS


class ApiKeyCreateResponse(BaseModel):
    """生成结果：api_key 仅此一次返回，请妥善保存"""
    api_key: str
    api_key_prefix: str
    api_key_created_at: object
    api_key_expires_at: object






