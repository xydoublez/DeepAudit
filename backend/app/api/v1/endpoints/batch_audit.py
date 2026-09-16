"""
定时批量审计 API

- 计划（AuditSchedule）：增删改查 + 立即触发
- 批次（AuditBatchRun）：列表 / 详情 / 明细分页 / 取消

权限：计划创建者本人或超级管理员。
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.config import settings
from app.db.session import get_db
from app.models.batch_audit import (
    AuditBatchRun,
    AuditBatchRunItem,
    AuditSchedule,
    BatchRunStatus,
    ProjectScope,
    ScheduleType,
)
from app.models.project import Project
from app.models.user import User
from app.services.scheduler.batch_runner import cancel_batch_run
from app.services.scheduler.next_run import compute_next_run, describe_schedule
from app.services.scheduler.scheduler import batch_audit_scheduler

logger = logging.getLogger(__name__)
router = APIRouter()


# ============ Schemas ============

class ScheduleTaskTemplate(BaseModel):
    """审计参数模板（创建 AgentTask 时使用）"""
    target_vulnerabilities: Optional[List[str]] = Field(None, description="目标漏洞类型")
    verification_level: str = Field("sandbox", description="验证级别")
    exclude_patterns: Optional[List[str]] = Field(None, description="排除模式")
    target_files: Optional[List[str]] = Field(None, description="指定扫描文件")
    max_iterations: int = Field(50, ge=1, le=200, description="最大迭代次数")
    timeout_seconds: int = Field(1800, ge=60, le=7200, description="单项目超时（秒）")
    branch_name: Optional[str] = Field(None, description="分支名称，留空则用各项目默认分支")


class ScheduleCreate(BaseModel):
    """创建定时批量审计计划"""
    name: str = Field(..., min_length=1, max_length=255, description="计划名称")
    description: Optional[str] = Field(None, description="计划描述")

    project_scope: str = Field(ProjectScope.ALL, description="项目范围: all / selected")
    project_ids: Optional[List[str]] = Field(None, description="scope=selected 时的项目 ID 列表")

    concurrency: int = Field(5, ge=1, description="并发审计的项目数")
    skip_if_running: bool = Field(True, description="上一批未跑完时跳过本次触发")

    schedule_type: str = Field(ScheduleType.DAILY, description="once/daily/weekly/interval/manual")
    schedule_config: Optional[Dict[str, Any]] = Field(None, description="调度参数")
    timezone: str = Field("Asia/Shanghai", max_length=64, description="时区名")

    task_template: Optional[ScheduleTaskTemplate] = Field(None, description="审计参数模板")
    enabled: bool = Field(True, description="是否启用")


class ScheduleUpdate(BaseModel):
    """修改定时批量审计计划（字段留空表示不修改）"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None

    project_scope: Optional[str] = None
    project_ids: Optional[List[str]] = None

    concurrency: Optional[int] = Field(None, ge=1)
    skip_if_running: Optional[bool] = None

    schedule_type: Optional[str] = None
    schedule_config: Optional[Dict[str, Any]] = None
    timezone: Optional[str] = Field(None, max_length=64)

    task_template: Optional[ScheduleTaskTemplate] = None
    enabled: Optional[bool] = None


# ============ 辅助函数 ============

def _require_access(record: Any, current_user: User, label: str) -> None:
    """校验访问权限：创建者本人或超级管理员"""
    if record.created_by != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail=f"无权操作此{label}")


def _validate_concurrency(concurrency: int) -> int:
    if concurrency > settings.BATCH_AUDIT_MAX_CONCURRENCY:
        raise HTTPException(
            status_code=400,
            detail=f"并发度不能超过 {settings.BATCH_AUDIT_MAX_CONCURRENCY}",
        )
    return concurrency


def _validate_scope(
    project_scope: str,
    project_ids: Optional[List[str]],
) -> Optional[List[str]]:
    if project_scope not in (ProjectScope.ALL, ProjectScope.SELECTED):
        raise HTTPException(status_code=400, detail="project_scope 只能是 all 或 selected")

    if project_scope == ProjectScope.SELECTED:
        # 保序去重：重复的 ID 会为同一项目生成多条批次明细，
        # 并发执行时同一仓库会被两个 AgentTask 同时审计
        seen: set = set()
        cleaned: List[str] = []
        for pid in project_ids or []:
            if isinstance(pid, str) and pid.strip() and pid not in seen:
                seen.add(pid)
                cleaned.append(pid)
        if not cleaned:
            raise HTTPException(status_code=400, detail="指定项目范围时必须选择至少一个项目")
        return cleaned

    return None


async def _assert_projects_owned(
    db: AsyncSession,
    project_ids: List[str],
    current_user: User,
) -> None:
    """校验所选项目均属于当前用户"""
    if not project_ids:
        return

    owned = (
        await db.execute(
            select(Project.id).where(
                Project.id.in_(project_ids),
                Project.owner_id == current_user.id,
            )
        )
    ).scalars().all()

    missing = set(project_ids) - set(owned)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"以下项目不存在或无权访问: {', '.join(sorted(missing)[:10])}",
        )


def _compute_next_run_or_400(
    schedule_type: str,
    schedule_config: Optional[Dict[str, Any]],
    tz_name: str,
    after: Optional[datetime] = None,
) -> Optional[datetime]:
    """计算下次执行时间，配置非法时返回 400"""
    if schedule_type not in (
        ScheduleType.ONCE, ScheduleType.DAILY, ScheduleType.WEEKLY,
        ScheduleType.INTERVAL, ScheduleType.MANUAL,
    ):
        raise HTTPException(status_code=400, detail=f"未知的调度类型: {schedule_type}")

    try:
        return compute_next_run(
            schedule_type,
            schedule_config,
            tz_name,
            after or datetime.now(timezone.utc),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"调度配置无效: {e}")


def _serialize_schedule(
    schedule: AuditSchedule,
    latest_run: Optional[AuditBatchRun] = None,
) -> Dict[str, Any]:
    return {
        "id": schedule.id,
        "name": schedule.name,
        "description": schedule.description,
        "created_by": schedule.created_by,
        "project_scope": schedule.project_scope,
        "project_ids": schedule.project_ids or [],
        "project_count": len(schedule.project_ids or []) if schedule.project_scope == ProjectScope.SELECTED else None,
        "concurrency": schedule.concurrency,
        "skip_if_running": bool(schedule.skip_if_running),
        "schedule_type": schedule.schedule_type,
        "schedule_config": schedule.schedule_config or {},
        "schedule_description": describe_schedule(
            schedule.schedule_type, schedule.schedule_config, schedule.timezone
        ),
        "timezone": schedule.timezone,
        "task_template": schedule.task_template or {},
        "enabled": bool(schedule.enabled),
        "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
        "last_run_id": schedule.last_run_id,
        "latest_run": _serialize_run(latest_run) if latest_run else None,
        "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
        "updated_at": schedule.updated_at.isoformat() if schedule.updated_at else None,
    }


def _serialize_run(run: AuditBatchRun) -> Dict[str, Any]:
    total = run.total_projects or 0
    done = (run.completed_count or 0) + (run.failed_count or 0) + (run.skipped_count or 0)
    return {
        "id": run.id,
        "schedule_id": run.schedule_id,
        "created_by": run.created_by,
        "trigger_type": run.trigger_type,
        "status": run.status,
        "concurrency": run.concurrency,
        "total_projects": total,
        "queued_count": run.queued_count or 0,
        "running_count": run.running_count or 0,
        "completed_count": run.completed_count or 0,
        "failed_count": run.failed_count or 0,
        "skipped_count": run.skipped_count or 0,
        "progress": round(done / total * 100, 2) if total else 0.0,
        "error_message": run.error_message,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def _serialize_item(item: AuditBatchRunItem) -> Dict[str, Any]:
    return {
        "id": item.id,
        "run_id": item.run_id,
        "project_id": item.project_id,
        "project_name": item.project_name,
        "agent_task_id": item.agent_task_id,
        "status": item.status,
        "attempts": item.attempts or 0,
        "findings_count": item.findings_count or 0,
        "error_message": item.error_message,
        "started_at": item.started_at.isoformat() if item.started_at else None,
        "finished_at": item.finished_at.isoformat() if item.finished_at else None,
    }


async def _get_owned_schedule(
    db: AsyncSession, schedule_id: str, current_user: User
) -> AuditSchedule:
    schedule = await db.get(AuditSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="计划不存在")
    _require_access(schedule, current_user, "计划")
    return schedule


async def _get_owned_run(db: AsyncSession, run_id: str, current_user: User) -> AuditBatchRun:
    run = await db.get(AuditBatchRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="批次不存在")
    _require_access(run, current_user, "批次")
    return run


# ============ 计划接口 ============

@router.post("/schedules")
async def create_schedule(
    payload: ScheduleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """创建定时批量审计计划"""
    project_ids = _validate_scope(payload.project_scope, payload.project_ids)
    await _assert_projects_owned(db, project_ids or [], current_user)
    _validate_concurrency(payload.concurrency)

    next_run_at = _compute_next_run_or_400(
        payload.schedule_type, payload.schedule_config, payload.timezone
    )

    schedule = AuditSchedule(
        id=str(uuid4()),
        name=payload.name.strip(),
        description=payload.description,
        created_by=current_user.id,
        project_scope=payload.project_scope,
        project_ids=project_ids,
        concurrency=payload.concurrency,
        skip_if_running=payload.skip_if_running,
        schedule_type=payload.schedule_type,
        schedule_config=payload.schedule_config or {},
        timezone=payload.timezone or "Asia/Shanghai",
        task_template=payload.task_template.model_dump() if payload.task_template else {},
        enabled=payload.enabled,
        next_run_at=next_run_at,
    )

    db.add(schedule)
    await db.commit()
    await db.refresh(schedule)

    logger.info(
        f"[BatchAudit] Created schedule '{schedule.name}' ({schedule.id}) "
        f"type={schedule.schedule_type} concurrency={schedule.concurrency} "
        f"next_run_at={schedule.next_run_at}"
    )
    return _serialize_schedule(schedule)


@router.get("/schedules")
async def list_schedules(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """计划列表（含下次执行时间与最近批次状态）"""
    conditions = []
    if not current_user.is_superuser:
        conditions.append(AuditSchedule.created_by == current_user.id)

    base_query = select(AuditSchedule)
    if conditions:
        base_query = base_query.where(*conditions)

    total = (
        await db.execute(select(func.count()).select_from(base_query.subquery()))
    ).scalar() or 0

    schedules = list(
        (
            await db.execute(
                base_query.order_by(AuditSchedule.created_at.desc()).offset(skip).limit(limit)
            )
        ).scalars().all()
    )

    # 批量取每个计划最近一次批次
    latest_runs: Dict[str, AuditBatchRun] = {}
    if schedules:
        schedule_ids = [s.id for s in schedules]
        rows = (
            await db.execute(
                select(AuditBatchRun)
                .where(AuditBatchRun.schedule_id.in_(schedule_ids))
                .order_by(AuditBatchRun.created_at.desc())
            )
        ).scalars().all()
        for row in rows:
            latest_runs.setdefault(row.schedule_id, row)

    return {
        "items": [_serialize_schedule(s, latest_runs.get(s.id)) for s in schedules],
        "total": total,
    }


@router.patch("/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: str,
    payload: ScheduleUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """修改计划（含启停）；调度参数变更后重算 next_run_at"""
    schedule = await _get_owned_schedule(db, schedule_id, current_user)
    changes = payload.model_dump(exclude_unset=True)

    if "concurrency" in changes and changes["concurrency"] is not None:
        _validate_concurrency(changes["concurrency"])

    if "project_scope" in changes or "project_ids" in changes:
        scope = changes.get("project_scope", schedule.project_scope)
        ids = changes.get("project_ids", schedule.project_ids)
        validated_ids = _validate_scope(scope, ids)
        await _assert_projects_owned(db, validated_ids or [], current_user)
        schedule.project_scope = scope
        schedule.project_ids = validated_ids

    for field in ("name", "description", "skip_if_running", "timezone", "enabled"):
        if field in changes and changes[field] is not None:
            setattr(schedule, field, changes[field])

    if "task_template" in changes:
        template = changes["task_template"]
        schedule.task_template = template if isinstance(template, dict) else {}

    # 调度参数变更 → 重算下次执行时间
    schedule_type = changes.get("schedule_type") or schedule.schedule_type
    schedule_config = changes.get("schedule_config")
    if schedule_config is None:
        schedule_config = schedule.schedule_config
    tz_name = changes.get("timezone") or schedule.timezone

    if "schedule_type" in changes:
        schedule.schedule_type = schedule_type
    if "schedule_config" in changes:
        schedule.schedule_config = schedule_config or {}

    schedule_changed = "schedule_type" in changes or "schedule_config" in changes or "timezone" in changes
    if schedule_changed or "enabled" in changes:
        schedule.next_run_at = _compute_next_run_or_400(
            schedule_type, schedule_config, tz_name
        )

    schedule.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(schedule)

    logger.info(f"[BatchAudit] Updated schedule {schedule.id}: {list(changes.keys())}")
    return _serialize_schedule(schedule)


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """删除计划（历史批次保留，schedule_id 置空）"""
    schedule = await _get_owned_schedule(db, schedule_id, current_user)

    await db.delete(schedule)
    await db.commit()

    logger.info(f"[BatchAudit] Deleted schedule {schedule_id}")
    return {"message": "计划已删除", "id": schedule_id}


@router.post("/schedules/{schedule_id}/trigger")
async def trigger_schedule(
    schedule_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """立即触发一次批量审计（不等待定时）"""
    await _get_owned_schedule(db, schedule_id, current_user)

    if not settings.BATCH_AUDIT_ENABLED:
        raise HTTPException(status_code=400, detail="批量审计调度器未启用（BATCH_AUDIT_ENABLED=false）")

    # 手动触发不得绕过「已有批次在跑」的检查（调度器内部的 skip_if_running 只对
    # 定时生效）：否则两个批次会覆盖同一批项目，同一仓库被并发审计
    # —— 重复克隆、findings 写两份、LLM 配额翻倍、共享 RAG collection 被并发读写
    active = (
        await db.execute(
            select(func.count(AuditBatchRun.id)).where(
                AuditBatchRun.schedule_id == schedule_id,
                AuditBatchRun.status.in_([BatchRunStatus.PENDING, BatchRunStatus.RUNNING]),
            )
        )
    ).scalar() or 0
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"该计划已有 {active} 个批次在执行中，请等待完成或先取消后再触发",
        )

    result = await batch_audit_scheduler.trigger_schedule(schedule_id)
    if not result:
        raise HTTPException(status_code=400, detail="触发失败，请稍后重试")

    logger.info(f"[BatchAudit] Manual trigger for schedule {schedule_id}: run={result['run_id']}")
    return result


# ============ 批次接口 ============

@router.get("/runs")
async def list_runs(
    schedule_id: Optional[str] = None,
    status: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """批次列表"""
    query = select(AuditBatchRun)

    if not current_user.is_superuser:
        query = query.where(AuditBatchRun.created_by == current_user.id)
    if schedule_id:
        query = query.where(AuditBatchRun.schedule_id == schedule_id)
    if status:
        query = query.where(AuditBatchRun.status == status)

    total = (
        await db.execute(
            select(func.count()).select_from(query.subquery())
        )
    ).scalar() or 0

    rows = (
        await db.execute(query.order_by(AuditBatchRun.created_at.desc()).offset(skip).limit(limit))
    ).scalars().all()

    return {"items": [_serialize_run(r) for r in rows], "total": total}


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """批次详情 + 实时进度计数"""
    run = await _get_owned_run(db, run_id, current_user)

    schedule_name = None
    if run.schedule_id:
        schedule = await db.get(AuditSchedule, run.schedule_id)
        schedule_name = schedule.name if schedule else None

    data = _serialize_run(run)
    data["schedule_name"] = schedule_name
    return data


@router.get("/runs/{run_id}/items")
async def list_run_items(
    run_id: str,
    status: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """批次明细分页（1000 个项目必须分页）"""
    await _get_owned_run(db, run_id, current_user)

    query = select(AuditBatchRunItem).where(AuditBatchRunItem.run_id == run_id)
    if status:
        query = query.where(AuditBatchRunItem.status == status)

    total = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar() or 0

    rows = (
        await db.execute(query.order_by(AuditBatchRunItem.created_at.asc()).offset(skip).limit(limit))
    ).scalars().all()

    return {"items": [_serialize_item(i) for i in rows], "total": total}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """取消批次：未出队项目直接标记取消，已在跑的逐个中断"""
    run = await _get_owned_run(db, run_id, current_user)

    if run.status in (
        BatchRunStatus.COMPLETED,
        BatchRunStatus.FAILED,
        BatchRunStatus.CANCELLED,
        BatchRunStatus.PARTIALLY_FAILED,
    ):
        raise HTTPException(status_code=400, detail="批次已结束，无法取消")

    result = await cancel_batch_run(run_id)

    logger.info(f"[BatchAudit] Cancel requested for run {run_id} by {current_user.id}")
    return {"message": "批次取消请求已提交", **result}
