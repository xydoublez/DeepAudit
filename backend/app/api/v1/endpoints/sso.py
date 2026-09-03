"""
Casdoor SSO 单点登录端点（后端代理模式 / 机密客户端）

后端处理完整的 OAuth 2.0 授权码 + PKCE 流程：换取 Casdoor 令牌与用户信息后，
在本地 User 表 upsert 用户并签发本地 JWT，与现有认证体系无缝兼容
（get_current_user / /users/me 无需任何改动）。

安全约束（对齐 casdoor-api.md）：
- 授权请求携带 state + nonce + code_challenge(S256)
- 回调恒定时间校验 state、校验 nonce、检查时效
- 临时状态用 SECRET_KEY 签名的 HttpOnly Cookie 承载，一次性使用后清除
- 回调响应 Cache-Control: no-store；令牌经 URL fragment 回传前端（不进日志/Referer）
"""

import logging
import secrets
from datetime import timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core import security
from app.core.config import settings
from app.db.session import get_db
from app.models.user import User
from app.services.sso import casdoor

logger = logging.getLogger(__name__)

router = APIRouter()

# 前端 SSO 回调页路径（相对前端站点根），令牌经 fragment 回传
_FRONTEND_CALLBACK_PATH = "/sso/callback"


def _cookie_secure() -> bool:
    """回调地址为 HTTPS 时对临时状态 Cookie 启用 Secure 属性。"""
    return (settings.CASDOOR_REDIRECT_URI or "").startswith("https://")


def _frontend_redirect(params: Dict[str, str]) -> RedirectResponse:
    """重定向回前端回调页，参数放在 URL fragment（不进入服务器日志/Referer）。

    同时清除临时状态 Cookie（一次性使用，防重放）并禁止缓存。
    """
    base = settings.CASDOOR_FRONTEND_URL.rstrip("/") + _FRONTEND_CALLBACK_PATH
    resp = RedirectResponse(url=f"{base}#{urlencode(params)}", status_code=302)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.delete_cookie(settings.CASDOOR_STATE_COOKIE_NAME, path="/")
    return resp


def _error_redirect(message: str) -> RedirectResponse:
    """携带错误信息重定向回前端回调页。"""
    return _frontend_redirect({"error": message})


async def _upsert_user(db: AsyncSession, userinfo: Dict[str, Any]) -> User:
    """按 email 在本地 upsert 用户，并按 Casdoor roles 映射本地角色。

    - email 缺失时以 sub 构造占位邮箱，保证唯一非空
    - roles 含 admin（不区分大小写）→ 本地 admin + superuser，否则 member
    - SSO 用户无本地密码，写入随机不可用哈希，杜绝密码登录绕过
    """
    sub = userinfo.get("sub") or userinfo.get("preferred_username")
    email = userinfo.get("email")
    if not email:
        if not sub:
            raise casdoor.CasdoorError(
                "用户信息缺少唯一标识", error_code="no_identifier"
            )
        email = f"{sub}@sso.casdoor.local"
        logger.warning("Casdoor 用户 %s 未返回 email，使用占位邮箱 %s", sub, email)

    display_name = (
        userinfo.get("name")
        or userinfo.get("real_name")
        or userinfo.get("preferred_username")
        or sub
    )
    avatar = userinfo.get("picture")
    phone = userinfo.get("phone")

    roles = userinfo.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    role_list: List[str] = [str(r) for r in roles]
    is_admin = any(r.lower() == "admin" for r in role_list)

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalars().first()

    if user:
        # 同步资料与角色（以 Casdoor 为准）
        if display_name:
            user.full_name = display_name
        if avatar:
            user.avatar_url = avatar
        if phone:
            user.phone = phone
        user.role = "admin" if is_admin else "member"
        user.is_superuser = is_admin
        logger.info("SSO 登录同步本地用户: %s (admin=%s)", email, is_admin)
    else:
        user = User(
            email=email,
            hashed_password=security.get_password_hash(secrets.token_urlsafe(32)),
            full_name=display_name,
            avatar_url=avatar,
            phone=phone,
            is_active=True,
            is_superuser=is_admin,
            role="admin" if is_admin else "member",
        )
        db.add(user)
        logger.info("SSO 自动创建本地用户: %s (admin=%s)", email, is_admin)

    await db.commit()
    await db.refresh(user)
    return user


@router.get("/sso/config")
async def sso_config() -> Any:
    """前端查询 SSO 是否启用及登录入口路径（不泄露任何机密）。"""
    enabled = casdoor.is_enabled()
    return {
        "enabled": enabled,
        "login_path": f"{settings.API_V1_STR}/auth/sso/login" if enabled else None,
    }


@router.get("/sso/login")
async def sso_login() -> Any:
    """步骤一：生成 PKCE + state + nonce，302 重定向到 Casdoor 授权端点。"""
    if not casdoor.is_enabled():
        return _error_redirect("单点登录未启用或配置不完整")

    code_verifier = casdoor.generate_code_verifier()
    code_challenge = casdoor.generate_code_challenge(code_verifier)
    state = casdoor.generate_random_string()
    nonce = casdoor.generate_random_string()

    try:
        authorize_url = casdoor.build_authorize_url(state, nonce, code_challenge)
    except casdoor.CasdoorError as exc:
        return _error_redirect(exc.message)

    state_token = casdoor.create_state_token(state, nonce, code_verifier)

    resp = RedirectResponse(url=authorize_url, status_code=302)
    # 临时状态存入 HttpOnly Cookie（禁止 JS 读取），随授权流程往返
    resp.set_cookie(
        key=settings.CASDOOR_STATE_COOKIE_NAME,
        value=state_token,
        max_age=settings.CASDOOR_STATE_TTL_SECONDS,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/sso/callback")
async def sso_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """步骤二+三+四：安全回调 → 换令牌 → 校验 nonce → 取用户信息 → 建立本地会话。"""
    if not casdoor.is_enabled():
        return _error_redirect("单点登录未启用或配置不完整")

    query = request.query_params

    # 1. 检查授权端点返回的错误
    auth_error: Optional[str] = query.get("error")
    if auth_error:
        logger.warning(
            "Casdoor 授权失败: %s - %s", auth_error, query.get("error_description")
        )
        return _error_redirect("授权失败，请重试")

    # 2. 取出并验签临时状态 Cookie
    state_token = request.cookies.get(settings.CASDOOR_STATE_COOKIE_NAME)
    if not state_token:
        return _error_redirect("登录状态已失效，请重新登录")
    session_state = casdoor.verify_state_token(state_token)
    if not session_state:
        return _error_redirect("登录状态校验失败，请重新登录")

    # 3. 校验 state（恒定时间比较，防 CSRF + timing attack）
    received_state = query.get("state", "")
    if not casdoor.timing_safe_compare(session_state.get("state"), received_state):
        logger.warning("SSO state 校验失败，可能存在 CSRF 攻击")
        return _error_redirect("安全校验失败，请重新登录")

    # 4. 提取授权码（有效期 5 分钟、一次性；临时状态时效由签名 JWT 的 exp 保障）
    code = query.get("code")
    if not code:
        return _error_redirect("未收到授权码")

    code_verifier = session_state.get("verifier")
    expected_nonce = session_state.get("nonce")

    try:
        # 5. 换取令牌（JSON Body + HTTP Basic Auth）
        token_data = await casdoor.exchange_code_for_token(code, code_verifier)
        access_token = token_data.get("access_token")
        if not access_token:
            return _error_redirect("未获取到访问令牌")

        # 6. 校验 nonce（从 id_token 提取并恒定时间比对，防令牌重放）
        id_token = token_data.get("id_token")
        if id_token and expected_nonce:
            claims = casdoor.extract_jwt_claims(id_token)
            if not casdoor.timing_safe_compare(claims.get("nonce"), expected_nonce):
                logger.warning("SSO nonce 校验失败，可能存在重放攻击")
                return _error_redirect("安全校验失败，请重新登录")

        # 7. 获取用户信息（Authorization: Bearer）
        userinfo = await casdoor.fetch_userinfo(access_token)

        # 8. 本地用户 upsert
        user = await _upsert_user(db, userinfo)
        if not user.is_active:
            return _error_redirect("用户已被禁用")

        # 9. 签发本地 JWT（与现有认证体系完全一致）
        local_token = security.create_access_token(
            user.id,
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        )
    except casdoor.CasdoorError as exc:
        logger.warning("SSO 回调处理失败: %s (%s)", exc.message, exc.error_code)
        return _error_redirect(exc.message)
    except Exception as exc:  # noqa: BLE001 - 兜底避免向用户暴露堆栈
        logger.exception("SSO 回调未预期错误: %s", exc)
        return _error_redirect("登录失败，请重试")

    # 10. 重定向回前端，携带本地令牌（fragment 传输 + no-store + 清除临时 Cookie）
    return _frontend_redirect({"access_token": local_token, "token_type": "bearer"})


@router.get("/sso/logout")
async def sso_logout() -> Any:
    """登出：清除临时 Cookie 并重定向到 Casdoor CAS 登出页，回跳前端登录页。

    ⚠ 使用 CAS 风格登出页而非 /api/logout（后者不做浏览器回跳，见文档 3.7 勘误）。
    """
    service = settings.CASDOOR_FRONTEND_URL.rstrip("/") + "/login"
    logout_url = casdoor.build_logout_url(service)
    resp = RedirectResponse(url=logout_url, status_code=302)
    resp.delete_cookie(settings.CASDOOR_STATE_COOKIE_NAME, path="/")
    resp.headers["Cache-Control"] = "no-store"
    return resp
