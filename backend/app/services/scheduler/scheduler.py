"""
定时批量审计调度器

进程内常驻协程实现（不引入 APScheduler / Celery）：

- `_tick_loop` 每 `BATCH_AUDIT_TICK_SECONDS` 秒轮询一次到期计划
- 到期即为该计划创建一个 `AuditBatchRun` 并交给 `batch_runner` 并发执行
- 启动时做一次恢复：把上次进程中断遗留的 running 批次标记为 failed，
  并为所有启用计划重算 `next_run_at`（开发模式 `--reload` 频繁重启时必须有）

⚠️ 单实例假设：多副本部署会重复触发，需引入分布式锁（Postgres advisory lock / Redis）。
当前部署形态（docker-compose 单 backend、uvicorn 单进程）无此问题。
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import func, select, update

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.batch_audit import (
    AuditBatchRun,
    AuditBatchRunItem,
    AuditSchedule,
    BatchRunItemStatus,
    BatchRunStatus,
    ScheduleType,
)

from .batch_runner import cancel_all_batches, run_batch, start_batch_run
from .next_run import compute_next_run

logger = logging.getLogger(__name__)

# 未结束的批次状态
_ACTIVE_RUN_STATUSES = (BatchRunStatus.PENDING, BatchRunStatus.RUNNING)


def _is_in_future(moment: Optional[datetime], now: datetime) -> bool:
    """判断时刻是否仍在未来（兼容驱动返回 naive 时间戳的情况）"""
    if moment is None:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment > now


class BatchAuditScheduler:
    """定时批量审计调度器（进程内单例）"""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ============ 生命周期 ============

    async def start(self) -> None:
        """启动调度器（幂等）"""
        if self.is_running:
            logger.debug("[Scheduler] Already running, skip start")
            return

        self._stopping.clear()

        try:
            await self._recover_on_startup()
        except Exception as e:
            logger.error(f"[Scheduler] Startup recovery failed: {e}", exc_info=True)

        self._task = asyncio.create_task(self._tick_loop(), name="batch-audit-scheduler")
        logger.info(
            f"[Scheduler] Batch audit scheduler started "
            f"(tick={settings.BATCH_AUDIT_TICK_SECONDS}s)"
        )

    async def stop(self) -> None:
        """停止调度器，并取消所有在跑的批次"""
        self._stopping.set()

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"[Scheduler] Error while stopping tick loop: {e}")
        self._task = None

        try:
            await cancel_all_batches("服务停机")
        except Exception as e:
            logger.warning(f"[Scheduler] Error while cancelling batches: {e}")

        logger.info("[Scheduler] Batch audit scheduler stopped")

    # ============ 轮询 ============

    async def _tick_loop(self) -> None:
        interval = max(5, int(settings.BATCH_AUDIT_TICK_SECONDS or 30))

        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
                break  # 收到停止信号
            except asyncio.TimeoutError:
                pass

            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 单轮异常不得终止常驻协程
                logger.error(f"[Scheduler] Tick failed: {e}", exc_info=True)

    async def _tick(self) -> None:
        """扫描到期计划并触发批次"""
        now = datetime.now(timezone.utc)
        started_run_ids: List[str] = []

        async with async_session_factory() as db:
            due_schedules = (
                await db.execute(
                    select(AuditSchedule).where(
                        AuditSchedule.enabled.is_(True),
                        AuditSchedule.next_run_at.is_not(None),
                        AuditSchedule.next_run_at <= now,
                    )
                )
            ).scalars().all()

            if not due_schedules:
                return

            logger.info(f"[Scheduler] {len(due_schedules)} schedule(s) due at {now.isoformat()}")

            for schedule in due_schedules:
                try:
                    run_id = await self._fire_schedule(db, schedule, now, trigger_type="scheduled")
                    if run_id:
                        started_run_ids.append(run_id)
                except Exception as e:
                    logger.error(
                        f"[Scheduler] Failed to fire schedule {schedule.id}: {e}", exc_info=True
                    )

            await db.commit()

        # 事务提交后再启动协程，确保批次记录对执行引擎可见
        for run_id in started_run_ids:
            start_batch_run(run_id)

    async def _fire_schedule(
        self,
        db,
        schedule: AuditSchedule,
        now: datetime,
        trigger_type: str,
        *,
        respect_skip_if_running: bool = True,
    ) -> Optional[str]:
        """为计划创建一个批次（调用方负责 commit）

        Returns:
            新建批次的 run_id；因 skip_if_running 被跳过时返回 None
        """
        if respect_skip_if_running and schedule.skip_if_running:
            active = (
                await db.execute(
                    select(func.count(AuditBatchRun.id)).where(
                        AuditBatchRun.schedule_id == schedule.id,
                        AuditBatchRun.status.in_(_ACTIVE_RUN_STATUSES),
                    )
                )
            ).scalar() or 0

            if active > 0:
                logger.info(
                    f"[Scheduler] Schedule '{schedule.name}' has {active} active run(s), "
                    f"skip this trigger"
                )
                if trigger_type == "scheduled":
                    self._reschedule(db, schedule, now, after_fire=True)
                return None

        run = AuditBatchRun(
            id=str(uuid4()),
            schedule_id=schedule.id,
            created_by=schedule.created_by,
            trigger_type=trigger_type,
            status=BatchRunStatus.PENDING,
            concurrency=max(1, int(schedule.concurrency or 1)),
        )
        db.add(run)
        await db.flush()

        schedule.last_run_at = now
        schedule.last_run_id = run.id
        # 手动触发不影响定时节奏，不重算 next_run_at
        if trigger_type == "scheduled":
            self._reschedule(db, schedule, now, after_fire=True)

        logger.info(
            f"[Scheduler] Created batch run {run.id} for schedule '{schedule.name}' "
            f"({trigger_type})"
        )
        return run.id

    def _reschedule(
        self, db, schedule: AuditSchedule, now: datetime, *, after_fire: bool = False
    ) -> None:
        """重算并写回计划的下次执行时间

        Args:
            after_fire: 是否刚刚真正触发过一轮。只有触发后才应停用一次性计划；
                启动恢复这类“仅重算时间”的场景必须传 False，否则会把尚未到期的
                一次性计划静默废掉（用户从未停用它，日志里也没有任何痕迹）。
        """
        try:
            schedule.next_run_at = compute_next_run(
                schedule.schedule_type,
                schedule.schedule_config,
                schedule.timezone,
                now,
            )
        except ValueError as e:
            logger.error(
                f"[Scheduler] Invalid schedule config for '{schedule.name}' "
                f"({schedule.id}): {e}; disabling it"
            )
            schedule.next_run_at = None
            schedule.enabled = False
            return

        if schedule.next_run_at is None:
            # compute_next_run 返回 None 表示已无可执行的未来时刻（一次性计划的
            # run_at 已过期），停用以免每轮 tick 反复扫到它
            if schedule.enabled:
                logger.info(
                    f"[Scheduler] Schedule '{schedule.name}' ({schedule.id}) has no future "
                    f"run time left; disabling it"
                )
            schedule.enabled = False
        elif after_fire and schedule.schedule_type == ScheduleType.ONCE:
            # 一次性计划触发后自动停用
            schedule.enabled = False
            schedule.next_run_at = None

    # ============ 手动触发 ============

    async def trigger_schedule(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        """立即触发一次批次（不等定时），返回批次概要"""
        now = datetime.now(timezone.utc)

        async with async_session_factory() as db:
            schedule = await db.get(AuditSchedule, schedule_id)
            if not schedule:
                return None

            run_id = await self._fire_schedule(
                db, schedule, now, trigger_type="manual", respect_skip_if_running=False
            )
            await db.commit()

        if not run_id:
            return None

        start_batch_run(run_id)
        return {"run_id": run_id, "schedule_id": schedule_id, "trigger_type": "manual"}

    # ============ 启动恢复 ============

    async def _recover_on_startup(self) -> None:
        """处理上次进程中断遗留的批次，并重算所有计划的下次执行时间"""
        now = datetime.now(timezone.utc)

        async with async_session_factory() as db:
            stale_runs = (
                await db.execute(
                    select(AuditBatchRun).where(AuditBatchRun.status.in_(_ACTIVE_RUN_STATUSES))
                )
            ).scalars().all()

            for run in stale_runs:
                run.status = BatchRunStatus.FAILED
                run.error_message = "服务重启导致批次中断"
                run.finished_at = now
                run.queued_count = 0
                run.running_count = 0

                await db.execute(
                    update(AuditBatchRunItem)
                    .where(
                        AuditBatchRunItem.run_id == run.id,
                        AuditBatchRunItem.status.in_(
                            [BatchRunItemStatus.QUEUED, BatchRunItemStatus.RUNNING]
                        ),
                    )
                    .values(
                        status=BatchRunItemStatus.CANCELLED,
                        finished_at=now,
                        error_message="服务重启导致中断",
                    )
                )

            if stale_runs:
                logger.warning(
                    f"[Scheduler] Marked {len(stale_runs)} interrupted batch run(s) as failed"
                )

            schedules = (
                await db.execute(select(AuditSchedule).where(AuditSchedule.enabled.is_(True)))
            ).scalars().all()

            recalculated = 0
            for schedule in schedules:
                # 只重算停机期间错过触发的计划。未到期的必须原样保留：
                # - interval 类型的下次时间是「基准 + 间隔」，重算等于每次重启都
                #   把倒计时顺延，频繁部署时该计划永远不会到期
                # - once 类型重算后会被判定为「已触发过」而停用
                if _is_in_future(schedule.next_run_at, now):
                    continue
                self._reschedule(db, schedule, now)
                recalculated += 1

            await db.commit()

            if recalculated:
                logger.info(
                    f"[Scheduler] Recalculated next_run_at for {recalculated} "
                    f"misfire schedule(s) out of {len(schedules)} enabled"
                )


# 进程内单例
batch_audit_scheduler = BatchAuditScheduler()


__all__ = ["BatchAuditScheduler", "batch_audit_scheduler", "run_batch", "start_batch_run"]
