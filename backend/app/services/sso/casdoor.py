"""
Casdoor SSO OAuth 2.0 客户端封装

实现 OAuth 2.0 Authorization Code Flow + PKCE + state + nonce，
严格遵循 casdoor-api.md 对接文档的安全约束。

安全要点：
- 所有客户端强制启用 PKCE(S256) + state + nonce
- state/nonce 使用恒定时间比较(hmac.compare_digest)防 timing attack
- 机密客户端 client_secret 通过 HTTP Basic Auth 传递（避免出现在 body/URL 日志）
- 临时状态用 SECRET_KEY 签名的短时效 JWT Cookie 承载（无状态、支持多实例部署）
- access_token 仅通过 Authorization 请求头传递
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
from jose import JWTError, jwt

from app.core.config import settings

logger = logging.getLogger(__name__)

# 承载 OAuth 临时状态的签名 JWT 使用的算法（与本地登录令牌隔离）
_STATE_ALGORITHM = "HS256"

# Casdoor 标准端点路径
_AUTHORIZE_PATH = "/login/oauth/authorize"
_TOKEN_PATH = "/api/login/oauth/access_token"
_USERINFO_PATH = "/api/userinfo"


class CasdoorError(Exception):
    """Casdoor SSO 流程异常。

    message 面向用户展示（不含敏感信息），error_code 用于日志/排查。
    """

    def __init__(self, message: str, error_code: Optional[str] = None) -> None:
        self.message = message
        self.error_code = error_code
        super().__init__(message)


# ---------------------------------------------------------------------------
# 配置检查
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    """判断 Casdoor SSO 是否已正确启用与配置。"""
    return bool(
        settings.CASDOOR_ENABLED
        and settings.CASDOOR_CLIENT_ID
        and settings.CASDOOR_CLIENT_SECRET
        and settings.CASDOOR_REDIRECT_URI
    )


def _require_enabled() -> None:
    if not is_enabled():
        raise CasdoorError(
            "Casdoor SSO 未启用或配置不完整", error_code="sso_not_configured"
        )


# ---------------------------------------------------------------------------
# PKCE / 随机串 / 恒定时间比较
# ---------------------------------------------------------------------------

def generate_code_verifier() -> str:
    """生成 43 字符的随机 code_verifier（RFC 7636）。"""
    return secrets.token_urlsafe(32)


def generate_code_challenge(verifier: str) -> str:
    """S256: base64url(sha256(verifier))，无填充。"""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_random_string() -> str:
    """生成 ≥32 字符的密码学安全随机串（用于 state / nonce）。"""
    return secrets.token_urlsafe(32)


def timing_safe_compare(a: Optional[str], b: Optional[str]) -> bool:
    """恒定时间字符串比较，防止 timing side-channel 攻击。"""
    if a is None or b is None:
        return False
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


# ---------------------------------------------------------------------------
# OAuth 临时状态签名（无状态，替代服务端 session）
# ---------------------------------------------------------------------------

def create_state_token(state: str, nonce: str, code_verifier: str) -> str:
    """将 state/nonce/code_verifier 打包为 SECRET_KEY 签名的短时效 JWT。

    存入 HttpOnly Cookie，回调时验签取出，天然防篡改、支持多实例、无需 Redis。
    """
    now = datetime.now(timezone.utc)
    payload = {
        "state": state,
        "nonce": nonce,
        "verifier": code_verifier,
        "iat": now,
        "exp": now + timedelta(seconds=settings.CASDOOR_STATE_TTL_SECONDS),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=_STATE_ALGORITHM)


def verify_state_token(token: str) -> Optional[Dict[str, Any]]:
    """验签并解析临时状态 JWT；失败或过期返回 None。"""
    try:
        return jwt.decode(
            token, settings.SECRET_KEY, algorithms=[_STATE_ALGORITHM]
        )
    except JWTError:
        return None


# ---------------------------------------------------------------------------
# 端点 URL 构造
# ---------------------------------------------------------------------------

def build_authorize_url(state: str, nonce: str, code_challenge: str) -> str:
    """构造 Casdoor 授权端点 URL（携带 PKCE + state + nonce）。"""
    _require_enabled()
    params = {
        "client_id": settings.CASDOOR_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": settings.CASDOOR_REDIRECT_URI,
        "scope": settings.CASDOOR_SCOPE,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{settings.CASDOOR_ENDPOINT}{_AUTHORIZE_PATH}?{urlencode(params)}"


def build_logout_url(service_url: str) -> str:
    """构造 CAS 风格浏览器登出回跳 URL。

    ⚠ 勿用 /api/logout 做浏览器跳转——该部署不消费 post_logout_redirect_uri
    且无浏览器重定向（见对接文档 3.7 勘误），回跳地址经 service 参数承载。
    """
    base = (
        f"{settings.CASDOOR_ENDPOINT}/cas/"
        f"{settings.CASDOOR_LOGOUT_OWNER}/{settings.CASDOOR_LOGOUT_APP}/logout"
    )
    return f"{base}?{urlencode({'service': service_url})}"


def extract_jwt_claims(token: str) -> Dict[str, Any]:
    """解析 JWT payload（不验签），用于提取 nonce 等 claim。

    令牌真实有效性由随后调用 /api/userinfo 在 Casdoor 服务端校验，
    此处仅做 nonce 一致性比对（防重放）。
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)  # base64url 补齐
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, TypeError) as exc:
        logger.warning("解析 id_token claims 失败: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# HTTP 调用：换取令牌 / 获取用户信息
# ---------------------------------------------------------------------------

def _safe_error_detail(resp: httpx.Response) -> str:
    """从错误响应中提取 error/error_description，不泄露敏感信息。"""
    try:
        data = resp.json()
        err = data.get("error", "unknown_error")
        desc = data.get("error_description")
        return f"{err}: {desc}" if desc else str(err)
    except ValueError:
        return f"HTTP {resp.status_code}"


async def exchange_code_for_token(code: str, code_verifier: str) -> Dict[str, Any]:
    """用授权码 + PKCE verifier 换取令牌（JSON Body + HTTP Basic Auth）。"""
    _require_enabled()
    url = f"{settings.CASDOOR_ENDPOINT}{_TOKEN_PATH}"
    body: Dict[str, Any] = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
    }
    headers = {"Content-Type": "application/json"}

    auth: Optional[tuple] = None
    if settings.CASDOOR_CLIENT_SECRET:
        # 机密客户端：client_id/secret 通过 HTTP Basic Auth 传递
        auth = (settings.CASDOOR_CLIENT_ID, settings.CASDOOR_CLIENT_SECRET)
    else:
        # 公开客户端兜底：仅在 body 传 client_id（无 secret）
        body["client_id"] = settings.CASDOOR_CLIENT_ID

    try:
        async with httpx.AsyncClient(
            timeout=settings.CASDOOR_HTTP_TIMEOUT
        ) as client:
            resp = await client.post(url, json=body, headers=headers, auth=auth)
    except httpx.RequestError as exc:
        logger.error("连接 Casdoor 令牌端点失败: %s", exc)
        raise CasdoorError("无法连接单点登录服务", error_code="network_error") from exc

    if resp.status_code != 200:
        detail = _safe_error_detail(resp)
        logger.warning("令牌换取失败: %s", detail)
        raise CasdoorError("授权码无效或已过期，请重新登录", error_code=detail)

    try:
        return resp.json()
    except ValueError as exc:
        raise CasdoorError("令牌端点返回数据格式异常", error_code="bad_json") from exc


async def fetch_userinfo(access_token: str) -> Dict[str, Any]:
    """调用 UserInfo 端点获取用户信息（Authorization: Bearer）。"""
    url = f"{settings.CASDOOR_ENDPOINT}{_USERINFO_PATH}"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(
            timeout=settings.CASDOOR_HTTP_TIMEOUT
        ) as client:
            resp = await client.get(url, headers=headers)
    except httpx.RequestError as exc:
        logger.error("连接 Casdoor 用户信息端点失败: %s", exc)
        raise CasdoorError("无法连接单点登录服务", error_code="network_error") from exc

    if resp.status_code != 200:
        logger.warning("获取用户信息失败: HTTP %s", resp.status_code)
        raise CasdoorError("获取用户信息失败", error_code=f"http_{resp.status_code}")

    try:
        return resp.json()
    except ValueError as exc:
        raise CasdoorError("用户信息返回数据格式异常", error_code="bad_json") from exc
