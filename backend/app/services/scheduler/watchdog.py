"""
Agent 任务看门狗（运行期僵尸对账）

执行协程 hang 死（git clone / Docker SDK / LLM 死锁 / 资源耗尽）时，
任务、批次明细、批次会永远停在 pending/running。启动恢复
（scheduler._recover_on_startup）只覆盖服务重启场景；本模块补齐运行期盲区，
周期扫描并强制收敛，保证「不重启服务也能自愈」：

1. AgentTask：pending/running 超过自身 timeout_seconds（含一个扫描周期缓冲）
   → 标记 failed。覆盖单任务路径（BackgroundTasks）hang、执行协程丢失、
   以及功能上线前遗留的历史僵尸。
2. AuditBatchRunItem：running 超过单项目超时硬上限 + 缓冲 → 标记 failed
   并修正批次计数（兜底 _execute_one 协程意外死亡的场景）。
3. AuditBatchRun：pending/running 但本进程已无活跃执行协程且超过缓冲
   → 收敛残留明细后按 _finalize_run 重算落终态（孤儿批次）。

幂等性：所有回收只作用于非终态记录，终态不可被覆盖。
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from sqlalchemy import select, update

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.agent_task import AgentTask, AgentTaskStatus
from app.models.batch_audit import (
    AuditBatchRun,
    AuditBatchRunItem,
    BatchRunItemStatus,
    BatchRunStatus,
)

logger = logging.getLogger(__name__)

# 单项目超时硬上限（与 AgentTaskCreate.timeout_seconds 的 le=7200 对齐）
MAX_TASK_TIMEOUT_SECONDS = 7200
# 孤儿批次回收缓冲（秒）：超过该时长仍无活跃协程的 pending/running 批次视为孤儿
ORPHAN_RUN_GRACE_SECONDS = 600

_task: Optional[asyncio.Task] = None


def watchdog_interval() -> int:
    raw = settings.AGENT_TASK_WATCHDOG_INTERVAL
    if raw is None:
        raw = 60
    return max(5, int(raw))


def watchdog_enabled() -> bool:
    """AGENT_TASK_WATCHDOG_INTERVAL <= 0 时禁用（注意不能用 `or` 兜底：0 会被吞成默认值）"""
    raw = settings.AGENT_TASK_WATCHDOG_INTERVAL
    return raw is not None and int(raw) > 0


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """兼容驱动返回 naive 时间戳的情况"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _age_seconds(moment: Optional[datetime], now: datetime) -> Optional[float]:
    moment = _as_utc(moment)
    if moment is None:
        return None
    return (now - moment).total_seconds()


# ============ 对账逻辑 ============

async def reconcile_once() -> Dict[str, int]:
    """执行一轮对账，返回各类回收计数（便于测试与观测）"""
    now = datetime.now(timezone.utc)
    interval = watchdog_interval()

    stats = {
        "reclaimed_tasks": await _reclaim_stale_agent_tasks(now, interval),
        "reclaimed_items": await _reclaim_stale_items(now),
        "finalized_runs": await _finalize_orphan_runs(now),
    }
    return stats


async def _reclaim_stale_agent_tasks(now: datetime, interval: int) -> int:
    """回收超时的 pending/running AgentTask

    阈值 = 任务自身 timeout_seconds + 一个扫描周期：正常批量任务会先被
    _execute_one 的超时保护回收（其消息更精确），看门狗只兜剩余场景，
    因此不会误杀仍在合法执行中的任务。
    """
    reclaimed = 0
    async with async_session_factory() as db:
        tasks = (
            await db.execute(
                select(AgentTask).where(
                    AgentTask.status.in_(
                        (AgentTaskStatus.PENDING, AgentTaskStatus.RUNNING)
                    )
                )
            )
        ).scalars().all()

        stale_ids = []
        for task in tasks:
            age = _age_seconds(task.started_at or task.created_at, now)
            timeout = int(task.timeout_seconds or 1800)
            if age is None or age <= timeout + interval:
                continue
            task.status = AgentTaskStatus.FAILED
            task.error_message = f"任务执行超时（>{timeout}s），已被看门狗强制回收"
            task.completed_at = now
            stale_ids.append(task.id)
            reclaimed += 1

        if reclaimed:
            await db.commit()

    # 尽力终止仍挂在本进程的僵尸协程：置取消标志，阻止其苏醒后
    # 把状态改回 completed，并让 Agent 尽快停止消耗 LLM 配额
    for task_id in stale_ids:
        try:
            from app.api.v1.endpoints.agent_tasks import request_agent_task_cancellation

            request_agent_task_cancellation(task_id)
        except Exception as e:
            logger.debug(f"[Watchdog] Cancellation request for {task_id} failed: {e}")

    if reclaimed:
        logger.warning(f"[Watchdog] Reclaimed {reclaimed} stale agent task(s)")
    return reclaimed


async def _reclaim_stale_items(now: datetime) -> int:
    """回收 running 超过单项目硬上限的批次明细（_execute_one 协程死亡的兜底）"""
    threshold = MAX_TASK_TIMEOUT_SECONDS + ORPHAN_RUN_GRACE_SECONDS
    reclaimed = 0

    async with async_session_factory() as db:
        items = (
            await db.execute(
                select(AuditBatchRunItem).where(
                    AuditBatchRunItem.status == BatchRunItemStatus.RUNNING
                )
            )
        ).scalars().all()

        stale_by_run: Dict[str, int] = {}
        for item in items:
            age = _age_seconds(item.started_at, now)
            if age is None or age <= threshold:
                continue
            item.status = BatchRunItemStatus.FAILED
            item.finished_at = now
            item.error_message = (
                f"审计超时（>{MAX_TASK_TIMEOUT_SECONDS}s），已被看门狗强制回收"
            )
            stale_by_run[item.run_id] = stale_by_run.get(item.run_id, 0) + 1
            reclaimed += 1

        if reclaimed:
            await db.commit()

    # 修正仍存活批次的计数（孤儿批次的计数由 _finalize_run 重算覆盖）
    from .batch_runner import _apply_counter_delta

    for run_id, count in stale_by_run.items():
        await _apply_counter_delta(
            run_id, running_count=-count, failed_count=count
        )

    if reclaimed:
        logger.warning(f"[Watchdog] Reclaimed {reclaimed} stale batch item(s)")
    return reclaimed


async def _finalize_orphan_runs(now: datetime) -> int:
    """收敛孤儿批次：pending/running 但本进程已无活跃执行协程且超过缓冲

    触发场景：批次协程异常退出但 _finalize_run 落库失败、gather 提前返回
    留下残留明细、功能上线前遗留的僵尸批次。明细已无任何协程会推进，
    QUEUED→CANCELLED、RUNNING→FAILED 后按明细实际状态重算批次终态。
    """
    from .batch_runner import _finalize_run, list_running_batch_ids

    live_ids = set(list_running_batch_ids())

    orphan_ids = []
    async with async_session_factory() as db:
        runs = (
            await db.execute(
                select(AuditBatchRun).where(
                    AuditBatchRun.status.in_(
                        (BatchRunStatus.PENDING, BatchRunStatus.RUNNING)
                    )
                )
            )
        ).scalars().all()

        for run in runs:
            age = _age_seconds(run.started_at or run.created_at, now)
            if run.id in live_ids or age is None:
                continue
            if age <= ORPHAN_RUN_GRACE_SECONDS:
                continue
            orphan_ids.append(run.id)

        for run_id in orphan_ids:
            await db.execute(
                update(AuditBatchRunItem)
                .where(
                    AuditBatchRunItem.run_id == run_id,
                    AuditBatchRunItem.status == BatchRunItemStatus.QUEUED,
                )
                .values(
                    status=BatchRunItemStatus.CANCELLED,
                    finished_at=now,
                    error_message="批次执行协程丢失，已被看门狗回收",
                )
            )
            await db.execute(
                update(AuditBatchRunItem)
                .where(
                    AuditBatchRunItem.run_id == run_id,
                    AuditBatchRunItem.status == BatchRunItemStatus.RUNNING,
                )
                .values(
                    status=BatchRunItemStatus.FAILED,
                    finished_at=now,
                    error_message="批次执行协程丢失，已被看门狗强制回收",
                )
            )
        if orphan_ids:
            await db.commit()

    finalized = 0
    for run_id in orphan_ids:
        try:
            await _finalize_run(run_id)
            finalized += 1
        except Exception as e:
            logger.warning(f"[Watchdog] Failed to finalize orphan run {run_id}: {e}")

    if finalized:
        logger.warning(f"[Watchdog] Finalized {finalized} orphan batch run(s)")
    return finalized


# ============ 生命周期 ============

async def _watchdog_loop() -> None:
    # 先睡后扫：启动恢复已由 scheduler 处理，避免与启动流程抢资源
    while True:
        await asyncio.sleep(watchdog_interval())
        try:
            await reconcile_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 单轮异常不得终止常驻协程
            logger.error(f"[Watchdog] Reconcile failed: {e}", exc_info=True)


async def start_watchdog() -> None:
    """启动看门狗（幂等；AGENT_TASK_WATCHDOG_INTERVAL <= 0 时禁用）"""
    global _task
    if not watchdog_enabled():
        logger.info("[Watchdog] Disabled (AGENT_TASK_WATCHDOG_INTERVAL <= 0)")
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_watchdog_loop(), name="agent-task-watchdog")
    logger.info(f"[Watchdog] Started (interval={watchdog_interval()}s)")


async def stop_watchdog() -> None:
    """停止看门狗（幂等）"""
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    finally:
        _task = None
    logger.info("[Watchdog] Stopped")
