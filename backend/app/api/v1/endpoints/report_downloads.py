"""
批量项目报告下载 API

- 通过 GitLab API 批量获取项目信息（PAT 认证）
- 创建批量下载任务（项目 ID 列表 / 存储路径 / 并发数 / 命名规则）
- 任务进度查询（总进度 / 当前下载项目 / 完成数 / 总数）
- 失败重试 / 断点续传 / 取消 / 日志 / 汇总报告

权限：任务创建者本人或超级管理员。
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.config import settings
from app.db.session import get_db
from app.models.project import Project
from app.models.report_download import (
    DownloadItemStatus,
    DownloadTaskStatus,
    FilenamePattern,
    ReportDownloadItem,
    ReportDownloadTask,
)
from app.models.user import User
from app.services.report_download import (
    GitLabApiError,
    GitLabClient,
    report_download_manager,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ============ Schemas ============

class GitLabProjectsRequest(BaseModel):
    """GitLab 项目信息批量获取请求"""
    gitlab_url: str = Field(..., min_length=4, description="GitLab 地址，如 https://gitlab.example.com")
    token: Optional[str] = Field(None, description="GitLab PAT，留空使用服务端配置")
    search: Optional[str] = Field(None, description="项目名搜索关键字")
    group: Optional[str] = Field(None, description="组 ID 或路径，只拉取该组下的项目")
    owned_only: bool = Field(False, description="只拉取 Token 所属用户自己的项目")
    skip_archived: bool = Field(True, description="跳过已归档项目")
    limit: int = Field(0, ge=0, le=5000, description="最多返回数量，0 表示不限制")


class GitLabProjectInfo(BaseModel):
    """GitLab 项目信息（含本地项目匹配结果）"""
    gitlab_id: int
    name: str
    path_with_namespace: Optional[str] = None
    namespace: Optional[str] = None
    web_url: Optional[str] = None
    default_branch: Optional[str] = None
    archived: bool = False
    matched_project_id: Optional[str] = None
    matched_project_name: Optional[str] = None


class GitLabMeta(BaseModel):
    """单个项目的 GitLab 附加信息（用于分类存储与汇总）"""
    gitlab_project_id: Optional[int] = None
    namespace: Optional[str] = None
    web_url: Optional[str] = None


class ReportDownloadCreate(BaseModel):
    """创建批量报告下载任务"""
    name: Optional[str] = Field(None, max_length=255, description="任务名称，默认自动生成")
    project_ids: List[str] = Field(..., min_length=1, description="DeepAudit 项目 ID 列表")
    gitlab_meta: Optional[Dict[str, GitLabMeta]] = Field(
        None, description="项目 ID → GitLab 信息映射（来自 GitLab 批量获取）"
    )
    output_dir: Optional[str] = Field(None, max_length=512, description="报告存储目录，留空使用默认目录")
    concurrency: int = Field(5, ge=1, description="并发下载数量")
    filename_pattern: str = Field(FilenamePattern.ID_NAME, description="命名规则: id_name / name / id")
    gitlab_url: Optional[str] = Field(None, max_length=512, description="来源 GitLab 地址（记录用）")


# ============ 辅助函数 ============

def _require_access(record: Any, current_user: User, label: str) -> None:
    """校验访问权限：创建者本人或超级管理员"""
    if record.created_by != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail=f"无权操作此{label}")


def _resolve_output_dir(task_id: str, custom: Optional[str]) -> str:
    """解析报告存储目录：相对路径挂在配置根目录下，禁止路径穿越"""
    if not custom or not custom.strip():
        return os.path.join(settings.REPORT_DOWNLOAD_OUTPUT_DIR, task_id)
    raw = custom.strip()
    if ".." in Path(raw).parts:
        raise HTTPException(status_code=400, detail="存储路径不允许包含 ..")
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(settings.REPORT_DOWNLOAD_OUTPUT_DIR, raw))


def _normalize_repo_url(url: Optional[str]) -> str:
    """规范化仓库地址用于匹配（去掉协议、末尾 .git 与斜杠）"""
    if not url:
        return ""
    cleaned = url.strip().lower()
    for prefix in ("https://", "http://", "ssh://", "git@"):
        cleaned = cleaned.replace(prefix, "")
    cleaned = cleaned.replace(":", "/")
    return cleaned.rstrip("/").removesuffix(".git")


def _task_to_dict(task: ReportDownloadTask) -> dict:
    done = (task.completed_count or 0) + (task.failed_count or 0) + (task.skipped_count or 0)
    total = task.total_count or 0
    progress = round(done / total * 100, 1) if total else 0.0
    return {
        "id": task.id,
        "name": task.name,
        "created_by": task.created_by,
        "output_dir": task.output_dir,
        "concurrency": task.concurrency,
        "filename_pattern": task.filename_pattern,
        "gitlab_url": task.gitlab_url,
        "total_count": task.total_count,
        "completed_count": task.completed_count,
        "failed_count": task.failed_count,
        "skipped_count": task.skipped_count,
        "current_items": task.current_items or [],
        "status": task.status,
        "running": report_download_manager.is_running(task.id),
        "progress": 100.0 if task.status == DownloadTaskStatus.COMPLETED else progress,
        "error_message": task.error_message,
        "summary_path": task.summary_path,
        "log_path": task.log_path,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


def _item_to_dict(item: ReportDownloadItem) -> dict:
    return {
        "id": item.id,
        "task_id": item.task_id,
        "project_id": item.project_id,
        "project_name": item.project_name,
        "gitlab_project_id": item.gitlab_project_id,
        "gitlab_namespace": item.gitlab_namespace,
        "gitlab_web_url": item.gitlab_web_url,
        "agent_task_id": item.agent_task_id,
        "status": item.status,
        "attempts": item.attempts,
        "error_message": item.error_message,
        "file_path": item.file_path,
        "file_size": item.file_size,
        "started_at": item.started_at.isoformat() if item.started_at else None,
        "finished_at": item.finished_at.isoformat() if item.finished_at else None,
    }


async def _get_task_or_404(db: AsyncSession, task_id: str, current_user: User) -> ReportDownloadTask:
    task = await db.get(ReportDownloadTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="下载任务不存在")
    _require_access(task, current_user, "下载任务")
    return task


def _read_log_tail(path: Optional[str], limit: int) -> List[str]:
    if not path or not Path(path).exists():
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        return [line.rstrip("\n") for line in lines[-limit:]]
    except OSError:
        return []


# ============ GitLab 项目批量获取 ============

@router.post("/gitlab/projects")
async def fetch_gitlab_projects(
    request: GitLabProjectsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """
    通过 GitLab API 批量获取项目信息（PAT 认证）

    返回 GitLab 项目清单，并自动匹配当前用户在 DeepAudit 中的项目
    （按仓库地址优先、项目名兜底），前端据此勾选要下载报告的项目。
    """
    token = request.token or settings.GITLAB_TOKEN
    if not token:
        raise HTTPException(status_code=400, detail="缺少 GitLab Access Token，请填写或配置服务端 GITLAB_TOKEN")

    client = GitLabClient(request.gitlab_url, token)
    try:
        gl_user = await client.validate_token()
        projects = await client.fetch_projects(
            search=request.search,
            group=request.group,
            owned_only=request.owned_only,
            skip_archived=request.skip_archived,
            limit=request.limit or None,
        )
    except GitLabApiError as exc:
        status = exc.status_code or 502
        detail = str(exc)
        if exc.is_auth_error:
            detail = f"GitLab 认证失败，请检查 Access Token: {detail}"
        raise HTTPException(status_code=400 if exc.is_auth_error else status, detail=detail)

    # 匹配本地项目：仓库地址优先，项目名兜底
    local_projects = (
        await db.execute(
            select(Project.id, Project.name, Project.repository_url).where(
                Project.owner_id == current_user.id
            )
        )
    ).all()
    by_repo: Dict[str, tuple] = {}
    by_name: Dict[str, tuple] = {}
    for pid, pname, repo_url in local_projects:
        by_repo[_normalize_repo_url(repo_url)] = (pid, pname)
        by_name.setdefault((pname or "").strip().lower(), (pid, pname))

    results: List[dict] = []
    matched = 0
    for gl in projects:
        match = by_repo.get(_normalize_repo_url(gl.get("web_url"))) or by_repo.get(
            _normalize_repo_url(gl.get("http_url_to_repo"))
        )
        if not match:
            match = by_name.get((gl.get("name") or "").strip().lower())
        path_ns = gl.get("path_with_namespace") or ""
        namespace = path_ns.rsplit("/", 1)[0] if "/" in path_ns else (path_ns or None)
        item = GitLabProjectInfo(
            gitlab_id=gl.get("id"),
            name=gl.get("name") or "",
            path_with_namespace=path_ns or None,
            namespace=namespace or None,
            web_url=gl.get("web_url"),
            default_branch=gl.get("default_branch"),
            archived=bool(gl.get("archived")),
            matched_project_id=match[0] if match else None,
            matched_project_name=match[1] if match else None,
        )
        if match:
            matched += 1
        results.append(item.model_dump())

    return {
        "gitlab_user": {"id": gl_user.get("id"), "username": gl_user.get("username")},
        "total": len(results),
        "matched": matched,
        "projects": results,
    }


# ============ 下载任务 ============

@router.post("")
async def create_report_download_task(
    request: ReportDownloadCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """创建批量报告下载任务并立即启动"""
    if request.concurrency > settings.REPORT_DOWNLOAD_MAX_CONCURRENCY:
        raise HTTPException(
            status_code=400,
            detail=f"并发下载数不能超过 {settings.REPORT_DOWNLOAD_MAX_CONCURRENCY}",
        )
    if request.filename_pattern not in (
        FilenamePattern.ID_NAME,
        FilenamePattern.NAME,
        FilenamePattern.ID,
    ):
        raise HTTPException(status_code=400, detail="filename_pattern 只能是 id_name / name / id")

    # 保序去重
    seen: set = set()
    project_ids: List[str] = []
    for pid in request.project_ids:
        if isinstance(pid, str) and pid.strip() and pid not in seen:
            seen.add(pid)
            project_ids.append(pid)
    if not project_ids:
        raise HTTPException(status_code=400, detail="项目 ID 列表不能为空")
    if len(project_ids) > 5000:
        raise HTTPException(status_code=400, detail="单次任务项目数不能超过 5000")

    owned = (
        await db.execute(
            select(Project.id, Project.name).where(
                Project.id.in_(project_ids),
                Project.owner_id == current_user.id,
            )
        )
    ).all()
    owned_map = {pid: pname for pid, pname in owned}
    missing = [pid for pid in project_ids if pid not in owned_map]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"以下项目不存在或不属于当前用户: {', '.join(missing[:10])}"
            + (" ..." if len(missing) > 10 else ""),
        )

    task = ReportDownloadTask(
        name=request.name or f"批量报告下载 {len(project_ids)} 个项目",
        created_by=current_user.id,
        output_dir="",  # 先占位，创建后按 task_id 生成
        concurrency=request.concurrency,
        filename_pattern=request.filename_pattern,
        gitlab_url=request.gitlab_url,
        total_count=len(project_ids),
        status=DownloadTaskStatus.PENDING,
    )
    db.add(task)
    await db.flush()
    task.output_dir = _resolve_output_dir(task.id, request.output_dir)

    gitlab_meta = request.gitlab_meta or {}
    items = [
        ReportDownloadItem(
            task_id=task.id,
            project_id=pid,
            project_name=owned_map[pid],
            gitlab_project_id=(
                gitlab_meta[pid].gitlab_project_id if pid in gitlab_meta else None
            ),
            gitlab_namespace=(
                gitlab_meta[pid].namespace if pid in gitlab_meta else None
            ),
            gitlab_web_url=(
                gitlab_meta[pid].web_url if pid in gitlab_meta else None
            ),
            status=DownloadItemStatus.QUEUED,
        )
        for pid in project_ids
    ]
    db.add_all(items)
    await db.commit()
    await db.refresh(task)

    logger.info(
        "用户 %s 创建批量报告下载任务 %s: %d 个项目, 并发 %d, 目录 %s",
        current_user.id, task.id, len(project_ids), task.concurrency, task.output_dir,
    )
    await report_download_manager.start(task.id, mode="fresh")

    return _task_to_dict(task)


@router.get("")
async def list_report_download_tasks(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """批量报告下载任务列表"""
    query = select(ReportDownloadTask).order_by(ReportDownloadTask.created_at.desc())
    count_query = select(func.count()).select_from(ReportDownloadTask)
    if not current_user.is_superuser:
        query = query.where(ReportDownloadTask.created_by == current_user.id)
        count_query = count_query.where(ReportDownloadTask.created_by == current_user.id)

    total = (await db.execute(count_query)).scalar() or 0
    tasks = (await db.execute(query.offset(skip).limit(limit))).scalars().all()
    return {"items": [_task_to_dict(t) for t in tasks], "total": total}


@router.get("/{task_id}")
async def get_report_download_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """任务详情 + 实时进度（总进度 / 当前下载项目 / 完成数 / 总数）"""
    task = await _get_task_or_404(db, task_id, current_user)
    return _task_to_dict(task)


@router.get("/{task_id}/items")
async def get_report_download_items(
    task_id: str,
    status: Optional[str] = Query(None, description="按状态过滤（如 failed）"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """任务明细分页（1000+ 项目必须分页），支持按状态过滤失败清单"""
    await _get_task_or_404(db, task_id, current_user)

    query = select(ReportDownloadItem).where(ReportDownloadItem.task_id == task_id)
    count_query = select(func.count()).select_from(ReportDownloadItem).where(
        ReportDownloadItem.task_id == task_id
    )
    if status:
        query = query.where(ReportDownloadItem.status == status)
        count_query = count_query.where(ReportDownloadItem.status == status)

    total = (await db.execute(count_query)).scalar() or 0
    items = (
        await db.execute(query.order_by(ReportDownloadItem.created_at).offset(skip).limit(limit))
    ).scalars().all()
    return {"items": [_item_to_dict(i) for i in items], "total": total}


@router.post("/{task_id}/retry")
async def retry_report_download_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """重试失败的项目（失败的明细重置为排队后继续下载）"""
    await _get_task_or_404(db, task_id, current_user)

    failed = (
        await db.execute(
            select(func.count()).select_from(ReportDownloadItem).where(
                ReportDownloadItem.task_id == task_id,
                ReportDownloadItem.status == DownloadItemStatus.FAILED,
            )
        )
    ).scalar() or 0
    if failed == 0:
        raise HTTPException(status_code=400, detail="没有失败的项目可重试")

    result = await report_download_manager.start(task_id, mode="retry")
    return {"message": result["message"], "task_id": task_id, "retry_count": failed}


@router.post("/{task_id}/resume")
async def resume_report_download_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """断点续传：继续未完成的明细，已完成且文件在盘的项目自动跳过"""
    await _get_task_or_404(db, task_id, current_user)

    result = await report_download_manager.start(task_id, mode="resume")
    task = await db.get(ReportDownloadTask, task_id)
    return {
        "message": result["message"],
        "task_id": task_id,
        "remaining": max(
            0,
            (task.total_count or 0)
            - (task.completed_count or 0)
            - (task.failed_count or 0)
            - (task.skipped_count or 0),
        ),
    }


@router.post("/{task_id}/cancel")
async def cancel_report_download_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """取消任务：停止拉取新项目，排队中的明细标记为已取消"""
    await _get_task_or_404(db, task_id, current_user)
    result = await report_download_manager.cancel(task_id)
    return {"message": result["message"], "task_id": task_id}


@router.get("/{task_id}/logs")
async def get_report_download_logs(
    task_id: str,
    limit: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """任务日志（运行中返回内存缓冲，结束后读取日志文件尾部）"""
    task = await _get_task_or_404(db, task_id, current_user)

    if report_download_manager.is_running(task_id):
        logs = report_download_manager.recent_logs(task_id, limit=limit)
        return {"task_id": task_id, "running": True, "logs": logs}

    return {
        "task_id": task_id,
        "running": False,
        "logs": [
            {"timestamp": None, "level": "INFO", "message": line}
            for line in _read_log_tail(task.log_path, limit)
        ],
    }


@router.get("/{task_id}/summary")
async def get_report_download_summary(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """汇总报告内容（成功数 / 失败清单及原因 / 跳过清单）"""
    task = await _get_task_or_404(db, task_id, current_user)

    if not task.summary_path or not Path(task.summary_path).exists():
        raise HTTPException(status_code=404, detail="汇总报告尚未生成（任务可能仍在进行中）")

    content = Path(task.summary_path).read_text(encoding="utf-8")
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="download_summary_{task_id[:8]}.md"'
        },
    )
