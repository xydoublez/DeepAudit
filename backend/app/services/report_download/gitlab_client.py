"""
GitLab REST API (v4) 异步客户端

用于批量报告下载前的项目信息批量获取：
- PAT 认证（PRIVATE-TOKEN 头，兼容 Bearer）
- 分页拉取项目清单（支持 search / group / 归属过滤 / 上限）
- 瞬时错误（429 / 5xx / 网络超时）指数退避重试
"""

import asyncio
import logging
from typing import Any, Dict, Iterator, List, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger("app.report_download.gitlab")


class GitLabApiError(Exception):
    """GitLab API 调用异常（附带 HTTP 状态码，便于上层区分原因）"""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_auth_error(self) -> bool:
        return self.status_code in (401, 403)


def normalize_api_url(url: str) -> str:
    """规范化 GitLab API 地址：去掉末尾斜杠，自动补 /api/v4"""
    base = (url or "").strip().rstrip("/")
    if not base:
        return ""
    if not base.endswith("/api/v4"):
        base = f"{base}/api/v4"
    return base


class GitLabClient:
    """GitLab API v4 异步客户端（PAT 认证）"""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        if not token:
            raise GitLabApiError("缺少 GitLab Access Token，无法进行 API 认证")
        self.api_base = normalize_api_url(base_url)
        if not self.api_base:
            raise GitLabApiError("缺少 GitLab API 地址")
        self.token = token
        self.timeout = timeout or settings.REPORT_DOWNLOAD_HTTP_TIMEOUT
        self.max_retries = max_retries if max_retries is not None else settings.REPORT_DOWNLOAD_HTTP_RETRIES
        self.transport = transport  # 测试注入用，生产为 None

    def _headers(self) -> Dict[str, str]:
        return {
            "PRIVATE-TOKEN": self.token,
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        """带重试的请求：429/5xx/网络错误指数退避重试，4xx 直接抛错"""
        url = f"{self.api_base}{path}"
        last_error: Optional[Exception] = None
        attempts = self.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    verify=False,  # 内网自签证书场景
                    trust_env=False,  # 绕过系统代理直连内网（与 import_gitlab_projects.py 一致）
                    transport=self.transport,
                ) as client:
                    response = await client.request(
                        method, url, params=params, headers=self._headers()
                    )
                if response.status_code in (429,) or response.status_code >= 500:
                    last_error = GitLabApiError(
                        f"GitLab API 瞬时错误 {response.status_code}: {path}",
                        status_code=response.status_code,
                    )
                elif response.status_code >= 400:
                    # 401/403/404 等确定性错误不重试
                    detail = response.text[:200]
                    raise GitLabApiError(
                        f"GitLab API 错误 {response.status_code}: {detail}",
                        status_code=response.status_code,
                    )
                else:
                    return response
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = GitLabApiError(f"GitLab API 网络错误: {exc}")

            if attempt < attempts:
                delay = min(2 ** attempt, 15)
                logger.warning(
                    "GitLab API 请求失败（第 %d/%d 次），%d 秒后重试: %s",
                    attempt, attempts, delay, last_error,
                )
                await asyncio.sleep(delay)

        raise last_error or GitLabApiError("GitLab API 请求失败")

    async def validate_token(self) -> Dict[str, Any]:
        """校验 Token 有效性（GET /user），返回认证用户信息"""
        response = await self._request("GET", "/user")
        return response.json()

    async def fetch_projects(
        self,
        search: Optional[str] = None,
        group: Optional[str] = None,
        owned_only: bool = False,
        skip_archived: bool = True,
        limit: Optional[int] = None,
        page_size: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        分页批量获取项目信息

        - group 指定时走 /groups/{id}/projects，否则走 /projects?membership=true
        - 返回精简字段：id / name / path_with_namespace / web_url / default_branch /
          archived / last_activity_at / http_url_to_repo
        """
        fields = (
            "id,name,path_with_namespace,web_url,default_branch,"
            "archived,last_activity_at,http_url_to_repo"
        )
        results: List[Dict[str, Any]] = []
        page = 1

        while True:
            if group:
                path = f"/groups/{group}/projects"
                params: Dict[str, Any] = {
                    "per_page": page_size,
                    "page": page,
                    "simple": "true",
                    "order_by": "last_activity_at",
                }
            else:
                path = "/projects"
                params = {
                    "membership": "true" if not owned_only else "false",
                    "owned": "true" if owned_only else "false",
                    "simple": "true",
                    "per_page": page_size,
                    "page": page,
                    "order_by": "last_activity_at",
                }
            if search:
                params["search"] = search
            if skip_archived:
                params["archived"] = "false"

            response = await self._request("GET", path, params=params)
            batch: List[Dict[str, Any]] = response.json()
            if not batch:
                break

            for project in batch:
                # 只保留需要的字段，避免响应体过大
                results.append({k: project.get(k) for k in fields.split(",")})

            if limit is not None and len(results) >= limit:
                results = results[:limit]
                break

            # x-next-page 为空表示最后一页
            next_page = response.headers.get("x-next-page", "")
            if not next_page:
                break
            page = int(next_page)

        logger.info(
            "GitLab 项目清单获取完成: %d 个 (search=%s, group=%s)", len(results), search, group
        )
        return results

    async def fetch_project(self, project_id: int) -> Dict[str, Any]:
        """获取单个项目信息"""
        response = await self._request("GET", f"/projects/{project_id}")
        return response.json()


def iter_project_ids_from_text(text: str) -> Iterator[str]:
    """
    从用户粘贴的文本中解析项目 ID 列表（支持逗号/空格/换行分隔，自动去重保序）
    """
    seen: set = set()
    for chunk in text.replace(",", " ").replace(";", " ").split():
        pid = chunk.strip()
        if pid and pid not in seen:
            seen.add(pid)
            yield pid
