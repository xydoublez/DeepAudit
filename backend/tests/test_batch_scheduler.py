"""
批量审计调度器的重算语义测试

重点覆盖一个曾导致静默数据丢失的缺陷：`_reschedule` 原本无条件停用一次性计划，
而 `_recover_on_startup` 又对所有启用计划全量重算，于是**服务每次重启都会把尚未
到期的一次性计划废掉**（用户从未停用它，日志里也没有任何痕迹），同时把 interval
计划的倒计时顺延（频繁部署时该计划永远不会到期）。

修复后的契约：
- `_reschedule(after_fire=True)`：刚刚真正触发过 -> 一次性计划停用
- `_reschedule(after_fire=False)`：仅重算时间 -> 一次性计划保持启用
- `_recover_on_startup`：只重算已错过触发的计划，未到期的原样保留
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, List, Optional
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.sql.dml import Update

from app.models.batch_audit import AuditBatchRun, AuditSchedule, ScheduleType
from app.services.scheduler import scheduler as scheduler_module
from app.services.scheduler.scheduler import BatchAuditScheduler, _is_in_future

SHANGHAI = "Asia/Shanghai"


def _schedule(
    *,
    schedule_type: str = ScheduleType.DAILY,
    schedule_config: Optional[dict] = None,
    next_run_at: Optional[datetime] = None,
    timezone_name: str = SHANGHAI,
    enabled: bool = True,
) -> SimpleNamespace:
    """构造一个只有属性的假 AuditSchedule（_reschedule 不碰数据库）"""
    return SimpleNamespace(
        id="sch-test-0001",
        name="测试计划",
        schedule_type=schedule_type,
        schedule_config=schedule_config or {},
        timezone=timezone_name,
        enabled=enabled,
        next_run_at=next_run_at,
        skip_if_running=False,
    )


class _FakeResult:
    def __init__(self, rows: Optional[List[Any]] = None, rowcount: int = 0):
        self._rows = rows or []
        self.rowcount = rowcount

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> List[Any]:
        return self._rows


class _FakeSession:
    """按被查询的实体分发结果"""

    def __init__(self, stale_runs: List[Any], schedules: List[Any]):
        self._stale_runs = stale_runs
        self._schedules = schedules
        self.committed = 0

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def execute(self, stmt, *args, **kwargs) -> _FakeResult:
        if isinstance(stmt, Update):
            return _FakeResult(rowcount=0)

        descriptions = getattr(stmt, "column_descriptions", None) or []
        entity = descriptions[0].get("entity") if descriptions else None
        if entity is AuditBatchRun:
            return _FakeResult(self._stale_runs)
        if entity is AuditSchedule:
            return _FakeResult(self._schedules)
        return _FakeResult()

    async def commit(self) -> None:
        self.committed += 1


def _install_session(monkeypatch, stale_runs, schedules) -> _FakeSession:
    session = _FakeSession(stale_runs, schedules)
    monkeypatch.setattr(scheduler_module, "async_session_factory", lambda: session)
    return session


# ============ _is_in_future ============

class TestIsInFuture:
    def test_none_is_not_in_future(self):
        assert _is_in_future(None, datetime.now(timezone.utc)) is False

    def test_aware_future(self):
        now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        assert _is_in_future(now + timedelta(hours=1), now) is True

    def test_aware_past(self):
        now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        assert _is_in_future(now - timedelta(hours=1), now) is False

    def test_naive_is_treated_as_utc(self):
        """驱动返回 naive 时间戳时不得抛异常，按 UTC 解释"""
        now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        assert _is_in_future(datetime(2026, 9, 16, 13, 0), now) is True
        assert _is_in_future(datetime(2026, 9, 16, 11, 0), now) is False

    def test_non_utc_timezone_is_compared_correctly(self):
        now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        # 上海 21:00 = UTC 13:00，仍在未来
        shanghai = ZoneInfo(SHANGHAI)
        assert _is_in_future(datetime(2026, 9, 16, 21, 0, tzinfo=shanghai), now) is True


# ============ _reschedule ============

class TestReschedule:
    """db 参数在 _reschedule 内未被使用（只改 ORM 对象属性），传 None 即可"""

    def test_startup_does_not_disable_future_once_schedule(self):
        """🔴 回归：启动恢复不得废掉尚未到期的一次性计划"""
        run_at = datetime(2026, 9, 17, 23, 0, tzinfo=ZoneInfo(SHANGHAI))
        sch = _schedule(
            schedule_type=ScheduleType.ONCE,
            schedule_config={"run_at": run_at.isoformat()},
            next_run_at=run_at.astimezone(timezone.utc),
        )
        # 服务在触发前一小时重启
        now = run_at.astimezone(timezone.utc) - timedelta(hours=1)

        BatchAuditScheduler()._reschedule(None, sch, now)

        assert sch.enabled is True, "一次性计划被静默停用了"
        assert sch.next_run_at == run_at.astimezone(timezone.utc)

    def test_after_fire_disables_once_schedule(self):
        """真正触发过之后，一次性计划才应停用"""
        run_at = datetime(2026, 9, 17, 23, 0, tzinfo=ZoneInfo(SHANGHAI))
        sch = _schedule(
            schedule_type=ScheduleType.ONCE,
            schedule_config={"run_at": run_at.isoformat()},
            next_run_at=run_at.astimezone(timezone.utc),
        )

        BatchAuditScheduler()._reschedule(None, sch, run_at.astimezone(timezone.utc), after_fire=True)

        assert sch.enabled is False
        assert sch.next_run_at is None

    def test_expired_once_schedule_is_disabled(self):
        """run_at 已过期 -> compute_next_run 返回 None -> 停用，避免每轮 tick 空转"""
        run_at = datetime(2026, 9, 15, 23, 0, tzinfo=ZoneInfo(SHANGHAI))
        sch = _schedule(
            schedule_type=ScheduleType.ONCE,
            schedule_config={"run_at": run_at.isoformat()},
            next_run_at=run_at.astimezone(timezone.utc),
        )

        BatchAuditScheduler()._reschedule(
            None, sch, datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        )

        assert sch.enabled is False
        assert sch.next_run_at is None

    def test_daily_advances_to_next_day_after_fire(self):
        sch = _schedule(
            schedule_type=ScheduleType.DAILY,
            schedule_config={"time": "15:00"},
            next_run_at=datetime(2026, 9, 16, 7, 0, tzinfo=timezone.utc),
        )

        # 上海 2026-09-16 15:30（已过当天的 15:00）
        now = datetime(2026, 9, 16, 15, 30, tzinfo=ZoneInfo(SHANGHAI))
        BatchAuditScheduler()._reschedule(None, sch, now, after_fire=True)

        assert sch.enabled is True
        # 下一次是 09-17 15:00 上海 = 07:00 UTC
        assert sch.next_run_at == datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc)

    def test_interval_is_measured_from_given_moment(self):
        sch = _schedule(
            schedule_type=ScheduleType.INTERVAL,
            schedule_config={"interval_hours": 6},
            next_run_at=datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc),
        )

        now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        BatchAuditScheduler()._reschedule(None, sch, now, after_fire=True)

        assert sch.enabled is True
        assert sch.next_run_at == datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc)

    def test_invalid_config_disables_schedule(self):
        sch = _schedule(
            schedule_type=ScheduleType.DAILY,
            schedule_config={"time": "不是时间"},
            next_run_at=datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc),
        )

        BatchAuditScheduler()._reschedule(
            None, sch, datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        )

        assert sch.enabled is False
        assert sch.next_run_at is None


# ============ _recover_on_startup ============

class TestRecoverOnStartup:
    async def test_future_schedules_are_left_untouched(self, monkeypatch):
        """🔴 回归：未到期的计划在启动恢复时必须原样保留

        一次性计划不能被停用，interval 计划的倒计时不能被顺延。
        """
        now = datetime.now(timezone.utc)
        once_at = now + timedelta(hours=6)

        once_future = _schedule(
            schedule_type=ScheduleType.ONCE,
            schedule_config={"run_at": once_at.astimezone(ZoneInfo(SHANGHAI)).isoformat()},
            next_run_at=once_at,
        )
        interval_future = _schedule(
            schedule_type=ScheduleType.INTERVAL,
            schedule_config={"interval_hours": 24},
            next_run_at=now + timedelta(hours=3),
        )
        session = _install_session(
            monkeypatch, stale_runs=[], schedules=[once_future, interval_future]
        )

        await BatchAuditScheduler()._recover_on_startup()

        assert session.committed == 1
        assert once_future.enabled is True
        assert once_future.next_run_at == once_at
        # interval 的下次时间没有被推后 24 小时
        assert interval_future.enabled is True
        assert interval_future.next_run_at == now + timedelta(hours=3)

    async def test_recalculating_everything_would_push_interval_forward(self, monkeypatch):
        """反证：跳过未到期计划的判断是必要的，不是空转代码

        模拟修复前的「全量重算」行为，interval 计划的倒计时会被顺延一个
        完整周期；每天部署一次 + interval_hours=24 时该计划永远不会到期。
        """
        now = datetime.now(timezone.utc)
        original_next = now + timedelta(hours=3)
        interval_future = _schedule(
            schedule_type=ScheduleType.INTERVAL,
            schedule_config={"interval_hours": 24},
            next_run_at=original_next,
        )
        monkeypatch.setattr(scheduler_module, "_is_in_future", lambda *a, **kw: False)
        _install_session(monkeypatch, stale_runs=[], schedules=[interval_future])

        await BatchAuditScheduler()._recover_on_startup()

        assert interval_future.next_run_at > now + timedelta(hours=23)
        assert interval_future.next_run_at != original_next

    async def test_misfire_schedules_are_recalculated(self, monkeypatch):
        """停机期间错过触发的计划要重算，否则会立刻补触发或永远停在过期时刻"""
        now = datetime.now(timezone.utc)

        daily_missed = _schedule(
            schedule_type=ScheduleType.DAILY,
            schedule_config={"time": "15:00"},
            next_run_at=now - timedelta(hours=2),
        )
        interval_missed = _schedule(
            schedule_type=ScheduleType.INTERVAL,
            schedule_config={"interval_hours": 6},
            next_run_at=now - timedelta(minutes=30),
        )
        once_missed = _schedule(
            schedule_type=ScheduleType.ONCE,
            schedule_config={
                "run_at": (now - timedelta(hours=1))
                .astimezone(ZoneInfo(SHANGHAI))
                .isoformat()
            },
            next_run_at=now - timedelta(hours=1),
        )
        never_scheduled = _schedule(
            schedule_type=ScheduleType.DAILY,
            schedule_config={"time": "09:00"},
            next_run_at=None,
        )
        _install_session(
            monkeypatch,
            stale_runs=[],
            schedules=[daily_missed, interval_missed, once_missed, never_scheduled],
        )

        await BatchAuditScheduler()._recover_on_startup()

        # 每日 / 间隔计划被推进到未来
        assert daily_missed.enabled is True
        assert daily_missed.next_run_at > now
        assert interval_missed.enabled is True
        assert interval_missed.next_run_at > now
        # 从未算过时间的计划补算
        assert never_scheduled.next_run_at is not None
        # 错过的一次性计划已无未来时刻 -> 停用
        assert once_missed.enabled is False
        assert once_missed.next_run_at is None

    async def test_stale_runs_marked_failed_and_items_cancelled(self, monkeypatch):
        """上次进程中断遗留的批次要标记失败，计数归零"""
        stale = SimpleNamespace(
            id="run-stale-0001",
            status="running",
            error_message=None,
            finished_at=None,
            queued_count=900,
            running_count=5,
        )
        session = _install_session(monkeypatch, stale_runs=[stale], schedules=[])

        await BatchAuditScheduler()._recover_on_startup()

        assert stale.status == "failed"
        assert stale.error_message == "服务重启导致批次中断"
        assert stale.finished_at is not None
        assert stale.queued_count == 0
        assert stale.running_count == 0
        assert session.committed == 1
