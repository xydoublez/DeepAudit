"""
批量审计执行引擎

负责一次批次（AuditBatchRun）内所有项目的并发执行：

- 用 `asyncio.Semaphore(concurrency)` 控制「同时审计 N 个项目」，其余项目排队
- 直接 `await _execute_agent_task(task_id)`（不走 BackgroundTasks），
  该函数内部自建数据库会话，天然支持并发
- 单项目失败不影响其他项目
- 每个项目状态变化即刻原子更新批次计数，前端轮询可见实时进度
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

from sqlalchemy import func, select, update

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.agent_task import AgentTask, AgentTaskPhase, AgentTaskStatus
from app.models.batch_audit import (
    AuditBatchRun,
    AuditBatchRunItem,
    AuditSchedule,
    BatchRunItemStatus,
    BatchRunStatus,
    ProjectScope,
)
from app.models.project import Project

logger = logging.getLogger(__name__)


# 已请求取消的批次 ID
_cancelled_runs: Set[str] = set()

# 正在执行的批次协程（run_id -> asyncio.Task），用于服务停机时优雅收尾
_running_batches: Dict[str, asyncio.Task] = {}


def is_run_cancelled(run_id: str) -> bool:
    """批次是否已被请求取消"""
    return run_id in _cancelled_runs


def list_running_batch_ids() -> List[str]:
    """当前进程内正在执行的批次 ID（用于监控/调试）"""
    return list(_running_batches.keys())


# ============ 计数更新 ============

async def _apply_counter_delta(run_id: str, **deltas: int) -> None:
    """以 SQL 原子增减的方式更新批次计数，避免并发下的读改写竞争"""
    if not deltas:
        return

    values: Dict[str, Any] = {}
    for field, delta in deltas.items():
        values[field] = getattr(AuditBatchRun, field) + delta

    try:
        async with async_session_factory() as db:
            await db.execute(
                update(AuditBatchRun).where(AuditBatchRun.id == run_id).values(**values)
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"[BatchRunner] Failed to update counters for run {run_id}: {e}")


async def _set_item_status(
    item_id: str,
    status: str,
    *,
    agent_task_id: Optional[str] = None,
    findings_count: Optional[int] = None,
    error_message: Optional[str] = None,
    started: bool = False,
    finished: bool = False,
) -> None:
    """更新单个批次明细的状态"""
    now = datetime.now(timezone.utc)
    values: Dict[str, Any] = {"status": status}

    if agent_task_id is not None:
        values["agent_task_id"] = agent_task_id
    if findings_count is not None:
        values["findings_count"] = findings_count
    if error_message is not None:
        values["error_message"] = error_message[:2000]
    if started:
        values["started_at"] = now
    if finished:
        values["finished_at"] = now

    try:
        async with async_session_factory() as db:
            await db.execute(
                update(AuditBatchRunItem).where(AuditBatchRunItem.id == item_id).values(**values)
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"[BatchRunner] Failed to update item {item_id}: {e}")


# ============ 项目清单解析 ============

async def _resolve_projects(
    created_by: str,
    scope: str,
    project_ids: Optional[List[str]],
) -> Tuple[List[Project], int]:
    """解析本批次要审计的项目清单

    Returns:
        (项目列表, 因已删除/无权限而被跳过的数量)
    """
    async with async_session_factory() as db:
        if scope != ProjectScope.SELECTED:
            # 全部项目：计划创建者名下所有启用项目
            result = await db.execute(
                select(Project)
                .where(
                    Project.owner_id == created_by,
                    Project.is_active.is_not(False),
                )
                .order_by(Project.name)
            )
            return list(result.scalars().all()), 0

        # 保序去重：重复的 project_id 会为同一项目生成多条明细，
        # 在并发闸门下导致同一仓库被两个 AgentTask 同时审计
        # （重复克隆、重复消耗 LLM 配额、findings 写两份、共享 RAG collection 并发读写）
        seen: Set[str] = set()
        requested: List[str] = []
        for pid in project_ids or []:
            if isinstance(pid, str) and pid and pid not in seen:
                seen.add(pid)
                requested.append(pid)
        if not requested:
            return [], 0

        result = await db.execute(
            select(Project).where(
                Project.id.in_(requested),
                Project.owner_id == created_by,
                Project.is_active.is_not(False),
            )
        )
        found = {p.id: p for p in result.scalars().all()}
        # 保持用户勾选顺序，缺失的（已删除/非本人）计入 skipped
        projects = [found[pid] for pid in requested if pid in found]
        return projects, len(requested) - len(projects)


# ============ 单项目执行 ============

async def _execute_one(
    semaphore: asyncio.Semaphore,
    run_id: str,
    item: "_ItemSnapshot",
    template: Dict[str, Any],
    created_by: str,
) -> None:
    """在并发闸门内执行单个项目的 Agent 审计

    取消处理必须包住信号量等待期：`asyncio.gather` 被 cancel 时，尚在排队的
    协程会在 `semaphore.acquire()` 直接抛出 CancelledError，若 handler 写在
    `async with` 内部就完全捕获不到，这些明细会永远停在 QUEUED。
    """
    item_id = item.id
    project_name = item.project_name or ""
    # 是否已拿到并发额度：决定被取消时计数该如何回滚
    acquired = False

    try:
        async with semaphore:
            acquired = True
            project_id = item.project_id

            # 出队时若批次已被取消，直接标记 cancelled
            if is_run_cancelled(run_id):
                await _set_item_status(item_id, BatchRunItemStatus.CANCELLED, finished=True)
                await _apply_counter_delta(
                    run_id, queued_count=-1, skipped_count=1
                )
                logger.info(f"[BatchRunner] Run {run_id} cancelled, skip project {project_name}")
                return

            await _set_item_status(item_id, BatchRunItemStatus.RUNNING, started=True)
            await _apply_counter_delta(run_id, queued_count=-1, running_count=1)

            agent_task_id: Optional[str] = None
            try:
                # 延迟导入，避免 api 层与服务层循环依赖
                from app.api.v1.endpoints.agent_tasks import (
                    _execute_agent_task,
                    is_task_cancelled,
                )

                task = await _create_agent_task(
                    project_id=project_id,
                    project_name=project_name,
                    run_id=run_id,
                    template=template,
                    created_by=created_by,
                )
                agent_task_id = task["id"]
                await _set_item_status(item_id, BatchRunItemStatus.RUNNING, agent_task_id=agent_task_id)

                # 直接 await：_execute_agent_task 内部自建会话，
                # 信号量保证「最多 concurrency 个项目同时审计，其余排队」
                await _execute_agent_task(agent_task_id)

                result = await _read_agent_task_result(agent_task_id)
                status = result["status"]

                if status == AgentTaskStatus.COMPLETED:
                    item_status = BatchRunItemStatus.COMPLETED
                    await _set_item_status(
                        item_id,
                        item_status,
                        findings_count=result["findings_count"],
                        finished=True,
                    )
                    await _apply_counter_delta(run_id, running_count=-1, completed_count=1)
                elif status == AgentTaskStatus.CANCELLED or is_task_cancelled(agent_task_id):
                    item_status = BatchRunItemStatus.CANCELLED
                    await _set_item_status(
                        item_id,
                        item_status,
                        error_message=result["error_message"],
                        finished=True,
                    )
                    await _apply_counter_delta(run_id, running_count=-1, skipped_count=1)
                else:
                    item_status = BatchRunItemStatus.FAILED
                    await _set_item_status(
                        item_id,
                        item_status,
                        findings_count=result["findings_count"],
                        error_message=result["error_message"] or f"审计任务状态: {status}",
                        finished=True,
                    )
                    await _apply_counter_delta(run_id, running_count=-1, failed_count=1)

                logger.info(
                    f"[BatchRunner] Run {run_id} project '{project_name}' -> {item_status}"
                )

            except Exception as e:
                logger.error(
                    f"[BatchRunner] Run {run_id} project '{project_name}' failed: {e}",
                    exc_info=True,
                )
                await _set_item_status(
                    item_id, BatchRunItemStatus.FAILED, error_message=str(e), finished=True
                )
                await _apply_counter_delta(run_id, running_count=-1, failed_count=1)

    except asyncio.CancelledError:
        # 已出队：回滚 running_count；仍在排队：计数从未变动，
        # queued_count 已由 cancel_batch_run 的批量 UPDATE 扣减，此处不重复扣
        if acquired:
            await _apply_counter_delta(run_id, running_count=-1, skipped_count=1)
        # 明细终态幂等写入，覆盖「批量 UPDATE 之后才出队」的竞态缺口
        await _set_item_status(
            item_id,
            BatchRunItemStatus.CANCELLED,
            error_message="批次被取消或服务停机",
            finished=True,
        )
        logger.info(f"[BatchRunner] Run {run_id} project '{project_name}' cancelled")
        raise


async def _create_agent_task(
    project_id: str,
    project_name: str,
    run_id: str,
    template: Dict[str, Any],
    created_by: str,
) -> Dict[str, Any]:
    """按计划的审计参数模板创建一条 AgentTask 记录"""
    async with async_session_factory() as db:
        project = await db.get(Project, project_id)
        branch_name = template.get("branch_name") or (
            project.default_branch if project else None
        )

        task = AgentTask(
            id=str(uuid4()),
            project_id=project_id,
            name=f"批量审计 - {project_name} - {datetime.now().strftime('%Y%m%d_%H%M%S')}",
            description=f"由定时批量审计触发（批次 {run_id[:8]}）",
            status=AgentTaskStatus.PENDING,
            current_phase=AgentTaskPhase.PLANNING,
            target_vulnerabilities=template.get("target_vulnerabilities")
            or ["sql_injection", "xss", "command_injection", "path_traversal", "ssrf"],
            verification_level=template.get("verification_level") or "sandbox",
            branch_name=branch_name,
            exclude_patterns=template.get("exclude_patterns"),
            target_files=template.get("target_files"),
            max_iterations=template.get("max_iterations") or 50,
            timeout_seconds=template.get("timeout_seconds") or 1800,
            created_by=created_by,
        )
        db.add(task)
        await db.commit()
        return {"id": task.id}


async def _read_agent_task_result(agent_task_id: str) -> Dict[str, Any]:
    """审计结束后回读 AgentTask 的最终状态"""
    async with async_session_factory() as db:
        task = await db.get(AgentTask, agent_task_id)
        if not task:
            return {
                "status": AgentTaskStatus.FAILED,
                "findings_count": 0,
                "error_message": "AgentTask 记录不存在",
            }
        return {
            "status": task.status,
            "findings_count": task.findings_count or 0,
            "error_message": task.error_message,
        }


# ============ 批次执行入口 ============

async def run_batch(run_id: str) -> None:
    """执行一个批次（由调度器或手动触发接口调用）

    本函数自身吞掉业务异常并把批次标记为 failed，
    只有外层显式 cancel 才会向上抛 CancelledError。
    """
    try:
        await _run_batch_inner(run_id)
    except asyncio.CancelledError:
        logger.warning(f"[BatchRunner] Run {run_id} cancelled")
        await _finalize_run(run_id, BatchRunStatus.CANCELLED, error_message="批次被取消")
        raise
    except Exception as e:
        logger.error(f"[BatchRunner] Run {run_id} crashed: {e}", exc_info=True)
        await _finalize_run(run_id, BatchRunStatus.FAILED, error_message=str(e))
    finally:
        _cancelled_runs.discard(run_id)
        _running_batches.pop(run_id, None)


async def _run_batch_inner(run_id: str) -> None:
    # 1. 读取批次与计划配置（全部转为普通值，不跨会话传递 ORM 实例）
    async with async_session_factory() as db:
        run = await db.get(AuditBatchRun, run_id)
        if not run:
            logger.error(f"[BatchRunner] Run {run_id} not found")
            return

        schedule: Optional[AuditSchedule] = None
        if run.schedule_id:
            schedule = await db.get(AuditSchedule, run.schedule_id)

        concurrency = max(1, int(run.concurrency or 1))
        created_by = run.created_by
        scope = schedule.project_scope if schedule else ProjectScope.ALL
        selected_ids = list(schedule.project_ids or []) if schedule else []
        template: Dict[str, Any] = dict((schedule.task_template if schedule else None) or {})

        run.status = BatchRunStatus.RUNNING
        run.started_at = datetime.now(timezone.utc)
        run.concurrency = concurrency
        await db.commit()

    # 2. 解析项目清单
    projects, skipped_missing = await _resolve_projects(created_by, scope, selected_ids)

    # 3. 批量插入明细
    snapshots: List["_ItemSnapshot"] = []
    async with async_session_factory() as db:
        for project in projects:
            item = AuditBatchRunItem(
                id=str(uuid4()),
                run_id=run_id,
                project_id=project.id,
                project_name=project.name,
                status=BatchRunItemStatus.QUEUED,
            )
            db.add(item)
            snapshots.append(
                _ItemSnapshot(id=item.id, project_id=project.id, project_name=project.name)
            )

        run = await db.get(AuditBatchRun, run_id)
        if run:
            run.total_projects = len(snapshots) + skipped_missing
            run.queued_count = len(snapshots)
            run.skipped_count = skipped_missing
        await db.commit()

    total = len(snapshots)
    logger.info(
        f"[BatchRunner] Run {run_id} started: {total} projects, "
        f"concurrency={concurrency}, skipped_missing={skipped_missing}"
    )

    if total == 0:
        await _finalize_run(
            run_id,
            BatchRunStatus.COMPLETED,
            error_message=None if skipped_missing else "没有匹配到可审计的项目",
        )
        return

    # 4. 并发执行（信号量保证同时最多 concurrency 个项目在审计）
    semaphore = asyncio.Semaphore(concurrency)
    results = await asyncio.gather(
        *[
            _execute_one(semaphore, run_id, snap, template, created_by)
            for snap in snapshots
        ],
        return_exceptions=True,
    )

    for err in results:
        if isinstance(err, asyncio.CancelledError):
            continue
        if isinstance(err, BaseException):
            logger.error(f"[BatchRunner] Run {run_id} unexpected item error: {err}")

    if any(isinstance(r, asyncio.CancelledError) for r in results):
        await _finalize_run(run_id, BatchRunStatus.CANCELLED, error_message="批次被取消")
        return

    # 5. 汇总批次最终状态
    await _finalize_run(run_id)


class _ItemSnapshot:
    """批次明细的轻量快照（脱离 ORM 会话后使用）"""

    __slots__ = ("id", "project_id", "project_name")

    def __init__(self, id: str, project_id: Optional[str], project_name: Optional[str]):
        self.id = id
        self.project_id = project_id
        self.project_name = project_name


async def _finalize_run(
    run_id: str,
    status: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """按明细实际状态重算计数并落库批次最终状态

    本函数是计数的**唯一权威**：运行期的 `_apply_counter_delta` 增量更新在
    「取消批次」路径下会与 `cancel_batch_run` 的批量 UPDATE 叠加而双计
    （两者之间存在 `await db.commit()` 让出事件循环的窗口），偏差必须在此校正。

    total_projects 在批次启动时写入且不再变动；skipped_count 需同时覆盖
    「无明细行的已删除/非本人项目」与「明细为 CANCELLED/SKIPPED」两部分，
    因此用 total_projects 减去明细总数反推前者，保证分项之和恒等于总数。
    """
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(AuditBatchRunItem.status, func.count(AuditBatchRunItem.id))
                .where(AuditBatchRunItem.run_id == run_id)
                .group_by(AuditBatchRunItem.status)
            )
        ).all()
        counts = {row[0]: int(row[1]) for row in rows}

        completed = counts.get(BatchRunItemStatus.COMPLETED, 0)
        failed = counts.get(BatchRunItemStatus.FAILED, 0)
        cancelled = counts.get(BatchRunItemStatus.CANCELLED, 0)
        skipped = counts.get(BatchRunItemStatus.SKIPPED, 0)
        queued = counts.get(BatchRunItemStatus.QUEUED, 0)
        running = counts.get(BatchRunItemStatus.RUNNING, 0)

        if status is None:
            if completed == 0 and failed > 0:
                status = BatchRunStatus.FAILED
            elif failed > 0 or cancelled > 0:
                status = BatchRunStatus.PARTIALLY_FAILED
            else:
                status = BatchRunStatus.COMPLETED

        run = await db.get(AuditBatchRun, run_id)
        if run:
            items_total = sum(counts.values())
            # 无明细行的那部分 skipped（已删除 / 非本人项目）由总数反推，
            # 避免重算时把它们冲掉
            baseline_skipped = max(0, int(run.total_projects or 0) - items_total)

            run.status = status
            run.queued_count = queued
            run.running_count = running
            run.completed_count = completed
            run.failed_count = failed
            run.skipped_count = baseline_skipped + cancelled + skipped
            run.finished_at = datetime.now(timezone.utc)
            if error_message:
                run.error_message = error_message[:2000]
            await db.commit()

        logger.info(
            f"[BatchRunner] Run {run_id} finished: status={status}, "
            f"completed={completed}, failed={failed}, skipped={skipped}, cancelled={cancelled}"
        )


# ============ 取消 ============

async def cancel_batch_run(run_id: str) -> Dict[str, Any]:
    """取消批次：未出队项目直接标记 cancelled，已在跑的逐个走 AgentTask 取消逻辑"""
    from app.api.v1.endpoints.agent_tasks import request_agent_task_cancellation

    if run_id in _running_batches:
        _cancelled_runs.add(run_id)
    else:
        # 本进程没有在跑的协程（已结束、或在 _finalize_run 报错后残留）。
        # 此时不能置取消标志：_cancelled_runs 的唯一清理点在 run_batch 的
        # finally，该协程不会再执行，条目会永久残留在内存里。
        # 仍继续走 DB 清理，以修正残留的 QUEUED/RUNNING 明细。
        logger.info(
            f"[BatchRunner] Run {run_id} has no live task in this process; "
            f"cancelling its pending items in DB only"
        )

    cancelled_tasks = 0
    async with async_session_factory() as db:
        # 正在跑的明细：请求取消对应的 AgentTask
        running_items = (
            await db.execute(
                select(AuditBatchRunItem).where(
                    AuditBatchRunItem.run_id == run_id,
                    AuditBatchRunItem.status == BatchRunItemStatus.RUNNING,
                )
            )
        ).scalars().all()

        for item in running_items:
            if item.agent_task_id:
                request_agent_task_cancellation(item.agent_task_id)
                cancelled_tasks += 1

        # 排队中的明细：直接标记取消，并同步计入 skipped_count
        queued_result = await db.execute(
            update(AuditBatchRunItem)
            .where(
                AuditBatchRunItem.run_id == run_id,
                AuditBatchRunItem.status == BatchRunItemStatus.QUEUED,
            )
            .values(
                status=BatchRunItemStatus.CANCELLED,
                finished_at=datetime.now(timezone.utc),
                error_message="批次已取消",
            )
        )
        queued_cancelled = queued_result.rowcount or 0
        if queued_cancelled:
            await db.execute(
                update(AuditBatchRun)
                .where(AuditBatchRun.id == run_id)
                .values(
                    queued_count=AuditBatchRun.queued_count - queued_cancelled,
                    skipped_count=AuditBatchRun.skipped_count + queued_cancelled,
                )
            )
        await db.commit()

    # 中断批次协程本体（等待中的项目不再出队）
    batch_task = _running_batches.get(run_id)
    if batch_task and not batch_task.done():
        batch_task.cancel()

    logger.info(
        f"[BatchRunner] Cancel requested for run {run_id}: "
        f"{cancelled_tasks} running agent task(s) interrupted, "
        f"{queued_cancelled} queued item(s) cancelled"
    )
    return {
        "run_id": run_id,
        "interrupted_tasks": cancelled_tasks,
        "cancelled_queued": queued_cancelled,
    }


def register_batch_task(run_id: str, task: asyncio.Task) -> None:
    """登记批次协程，便于取消与服务停机时收尾"""
    _running_batches[run_id] = task


def start_batch_run(run_id: str) -> asyncio.Task:
    """在当前事件循环上启动一个批次并登记

    必须在批次记录已提交后调用，否则执行引擎读不到该记录。
    """
    task = asyncio.create_task(run_batch(run_id), name=f"batch-run-{run_id[:8]}")
    register_batch_task(run_id, task)
    return task


async def cancel_all_batches(reason: str = "服务停机") -> None:
    """服务停机时取消所有在跑的批次

    等待必须带超时：Agent 可能卡在不可中断的同步调用里（git clone / Docker SDK /
    同步 LLM 请求，单项目超时上限可达 7200s）。无限等待会让 lifespan shutdown
    阻塞到容器宽限期耗尽后被 SIGKILL，反而丢掉全部收尾（批次状态、计数、
    临时目录）。超时的批次交由下次启动的 `_recover_on_startup` 对账。
    """
    for run_id in list(_running_batches.keys()):
        try:
            await cancel_batch_run(run_id)
        except Exception as e:
            # 单个批次取消失败不得阻断其余批次的收尾
            logger.warning(f"[BatchRunner] Failed to cancel run {run_id}: {e}")

    tasks = [t for t in _running_batches.values() if not t.done()]
    if not tasks:
        return

    timeout = max(1, int(settings.BATCH_AUDIT_SHUTDOWN_TIMEOUT or 25))
    logger.info(
        f"[BatchRunner] Waiting up to {timeout}s for {len(tasks)} batch task(s) "
        f"to finish: {reason}"
    )
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    if pending:
        logger.warning(
            f"[BatchRunner] {len(pending)} batch task(s) did not finish within {timeout}s; "
            f"they will be reconciled on next startup"
        )
