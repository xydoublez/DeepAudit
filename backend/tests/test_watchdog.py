"""
Agent 任务看门狗测试

复用 test_batch_runner 的 FakeSession 模式（环境无 aiosqlite），
直接驱动 reconcile_once 的三个对账分支：
① 超时的 pending/running AgentTask → failed
② running 超过硬上限的批次明细 → failed + 计数修正
③ 无活跃协程的孤儿批次 → 明细收敛 + _finalize_run 落终态
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest
from sqlalchemy.sql import Select
from sqlalchemy.sql.dml import Update

from app.models.agent_task import AgentTaskStatus
from app.models.batch_audit import BatchRunItemStatus, BatchRunStatus
from app.services.scheduler import batch_runner, watchdog


def _minutes_ago(minutes: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


class FakeResult:
    def __init__(self, rows: Optional[List[Any]] = None):
        self._rows = rows or []

    def scalars(self) -> "FakeResult":
        return self

    def all(self) -> List[Any]:
        return self._rows


class FakeSession:
    def __init__(self, handler):
        self._handler = handler
        self.statements: List[Any] = []
        self.committed = 0

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def execute(self, stmt, *args, **kwargs) -> FakeResult:
        self.statements.append(stmt)
        return self._handler(stmt)

    async def commit(self) -> None:
        self.committed += 1


@pytest.fixture(autouse=True)
async def _isolate():
    """每个测试前后重置模块级状态，并屏蔽对 agent_tasks 全局态的副作用"""
    yield
    if watchdog._task is not None:
        await watchdog.stop_watchdog()
    batch_runner._cancelled_runs.clear()
    batch_runner._running_batches.clear()


@pytest.fixture(autouse=True)
def _stub_cancellation(monkeypatch):
    monkeypatch.setattr(
        "app.api.v1.endpoints.agent_tasks.request_agent_task_cancellation",
        lambda task_id: None,
    )


def _install_session(monkeypatch, handler) -> List[FakeSession]:
    sessions: List[FakeSession] = []

    def factory():
        session = FakeSession(handler)
        sessions.append(session)
        return session

    monkeypatch.setattr(watchdog, "async_session_factory", factory)
    return sessions


# ============ ① 超时 AgentTask 回收 ============

class TestReclaimStaleAgentTasks:
    async def test_stale_task_marked_failed_and_cancel_requested(self, monkeypatch):
        cancelled: List[str] = []
        monkeypatch.setattr(
            "app.api.v1.endpoints.agent_tasks.request_agent_task_cancellation",
            cancelled.append,
        )

        stale = SimpleNamespace(
            id="task-stale",
            status=AgentTaskStatus.RUNNING,
            started_at=_minutes_ago(120),  # timeout 1800s + interval 60s = 31min
            created_at=_minutes_ago(120),
            timeout_seconds=1800,
            error_message=None,
            completed_at=None,
        )
        fresh = SimpleNamespace(
            id="task-fresh",
            status=AgentTaskStatus.RUNNING,
            started_at=_minutes_ago(10),
            created_at=_minutes_ago(10),
            timeout_seconds=1800,
            error_message=None,
            completed_at=None,
        )
        pending_forever = SimpleNamespace(  # 从未启动的僵尸：按 created_at 判定
            id="task-pending",
            status=AgentTaskStatus.PENDING,
            started_at=None,
            created_at=_minutes_ago(240),
            timeout_seconds=1800,
            error_message=None,
            completed_at=None,
        )

        def handler(stmt):
            assert isinstance(stmt, Select)
            return FakeResult(rows=[stale, fresh, pending_forever])

        sessions = _install_session(monkeypatch, handler)

        reclaimed = await watchdog._reclaim_stale_agent_tasks(
            datetime.now(timezone.utc), interval=60
        )

        assert reclaimed == 2
        assert stale.status == AgentTaskStatus.FAILED
        assert "超时" in stale.error_message
        assert stale.completed_at is not None
        assert pending_forever.status == AgentTaskStatus.FAILED
        # 未超时的任务不动
        assert fresh.status == AgentTaskStatus.RUNNING
        # 落库 + 对僵尸协程请求取消
        assert sessions[0].committed == 1
        assert sorted(cancelled) == ["task-pending", "task-stale"]

    async def test_custom_timeout_respected(self, monkeypatch):
        """长超时任务（7200s）在 31 分钟时不能被误杀"""
        long_task = SimpleNamespace(
            id="task-long",
            status=AgentTaskStatus.RUNNING,
            started_at=_minutes_ago(31),
            created_at=_minutes_ago(31),
            timeout_seconds=7200,
            error_message=None,
            completed_at=None,
        )

        def handler(stmt):
            return FakeResult(rows=[long_task])

        _install_session(monkeypatch, handler)

        reclaimed = await watchdog._reclaim_stale_agent_tasks(
            datetime.now(timezone.utc), interval=60
        )

        assert reclaimed == 0
        assert long_task.status == AgentTaskStatus.RUNNING

    async def test_naive_datetime_handled(self, monkeypatch):
        """驱动返回 naive 时间戳（如 SQLite）时同样可判定"""
        naive_task = SimpleNamespace(
            id="task-naive",
            status=AgentTaskStatus.RUNNING,
            started_at=datetime.utcnow() - timedelta(minutes=120),
            created_at=datetime.utcnow() - timedelta(minutes=120),
            timeout_seconds=1800,
            error_message=None,
            completed_at=None,
        )

        def handler(stmt):
            return FakeResult(rows=[naive_task])

        _install_session(monkeypatch, handler)

        reclaimed = await watchdog._reclaim_stale_agent_tasks(
            datetime.now(timezone.utc), interval=60
        )
        assert reclaimed == 1


# ============ ② 超时批次明细回收 ============

class TestReclaimStaleItems:
    async def test_stale_running_item_failed_with_counter_fix(self, monkeypatch):
        stale = SimpleNamespace(
            id="item-stale",
            run_id="run-1",
            status=BatchRunItemStatus.RUNNING,
            started_at=_minutes_ago(150),  # > 7200s + 600s 缓冲
            finished_at=None,
            error_message=None,
        )
        normal = SimpleNamespace(
            id="item-normal",
            run_id="run-1",
            status=BatchRunItemStatus.RUNNING,
            started_at=_minutes_ago(10),
            finished_at=None,
            error_message=None,
        )

        def handler(stmt):
            return FakeResult(rows=[stale, normal])

        sessions = _install_session(monkeypatch, handler)

        deltas: List[Any] = []

        async def fake_delta(run_id, **kwargs):
            deltas.append((run_id, kwargs))

        monkeypatch.setattr(batch_runner, "_apply_counter_delta", fake_delta)

        reclaimed = await watchdog._reclaim_stale_items(datetime.now(timezone.utc))

        assert reclaimed == 1
        assert stale.status == BatchRunItemStatus.FAILED
        assert stale.finished_at is not None
        assert "超时" in stale.error_message
        assert normal.status == BatchRunItemStatus.RUNNING  # 未超硬上限不动
        assert sessions[0].committed == 1
        # 批次计数被修正：running -1 / failed +1
        assert deltas == [("run-1", {"running_count": -1, "failed_count": 1})]

    async def test_fresh_running_item_untouched(self, monkeypatch):
        item = SimpleNamespace(
            id="item-ok",
            run_id="run-1",
            status=BatchRunItemStatus.RUNNING,
            started_at=_minutes_ago(30),
            finished_at=None,
            error_message=None,
        )

        def handler(stmt):
            return FakeResult(rows=[item])

        sessions = _install_session(monkeypatch, handler)

        reclaimed = await watchdog._reclaim_stale_items(datetime.now(timezone.utc))

        assert reclaimed == 0
        assert item.status == BatchRunItemStatus.RUNNING
        assert sessions[0].committed == 0  # 无回收不提交


# ============ ③ 孤儿批次收敛 ============

class TestFinalizeOrphanRuns:
    async def test_orphan_run_items_converged_and_finalized(self, monkeypatch):
        orphan = SimpleNamespace(
            id="run-orphan",
            status=BatchRunStatus.RUNNING,
            started_at=_minutes_ago(30),
            created_at=_minutes_ago(30),
        )
        live = SimpleNamespace(  # 本进程仍有活跃协程 → 不动
            id="run-live",
            status=BatchRunStatus.RUNNING,
            started_at=_minutes_ago(30),
            created_at=_minutes_ago(30),
        )
        young = SimpleNamespace(  # 无协程但刚创建 → 留缓冲
            id="run-young",
            status=BatchRunStatus.PENDING,
            started_at=None,
            created_at=_minutes_ago(1),
        )

        def handler(stmt):
            if isinstance(stmt, Select):
                return FakeResult(rows=[orphan, live, young])
            if isinstance(stmt, Update):
                assert stmt.table.name == "audit_batch_run_items"
                return FakeResult()
            raise AssertionError(f"未预期的语句: {stmt}")

        sessions = _install_session(monkeypatch, handler)

        finalized: List[str] = []

        async def fake_finalize(run_id, status=None, error_message=None):
            finalized.append(run_id)

        monkeypatch.setattr(batch_runner, "_finalize_run", fake_finalize)
        monkeypatch.setattr(batch_runner, "list_running_batch_ids", lambda: ["run-live"])

        count = await watchdog._finalize_orphan_runs(datetime.now(timezone.utc))

        assert count == 1
        assert finalized == ["run-orphan"]
        # QUEUED→CANCELLED 与 RUNNING→FAILED 两条批量 Update
        updates = [s for s in sessions[0].statements if isinstance(s, Update)]
        assert len(updates) == 2
        assert sessions[0].committed == 1

    async def test_exception_in_finalize_does_not_block_others(self, monkeypatch):
        orphans = [
            SimpleNamespace(
                id=f"run-{i}",
                status=BatchRunStatus.RUNNING,
                started_at=_minutes_ago(30),
                created_at=_minutes_ago(30),
            )
            for i in range(2)
        ]

        def handler(stmt):
            if isinstance(stmt, Select):
                return FakeResult(rows=orphans)
            return FakeResult()

        _install_session(monkeypatch, handler)

        finalized: List[str] = []

        async def flaky_finalize(run_id, status=None, error_message=None):
            if run_id == "run-0":
                raise RuntimeError("落库失败")
            finalized.append(run_id)

        monkeypatch.setattr(batch_runner, "_finalize_run", flaky_finalize)
        monkeypatch.setattr(batch_runner, "list_running_batch_ids", lambda: [])

        count = await watchdog._finalize_orphan_runs(datetime.now(timezone.utc))

        # 单个失败不阻断其余孤儿批次
        assert count == 1
        assert finalized == ["run-1"]


# ============ 汇总入口 ============

class TestReconcileOnce:
    async def test_reconcile_once_aggregates_stats(self, monkeypatch):
        async def fake_tasks(now, interval):
            return 3

        async def fake_items(now):
            return 2

        async def fake_runs(now):
            return 1

        monkeypatch.setattr(watchdog, "_reclaim_stale_agent_tasks", fake_tasks)
        monkeypatch.setattr(watchdog, "_reclaim_stale_items", fake_items)
        monkeypatch.setattr(watchdog, "_finalize_orphan_runs", fake_runs)

        stats = await watchdog.reconcile_once()

        assert stats == {"reclaimed_tasks": 3, "reclaimed_items": 2, "finalized_runs": 1}


# ============ 生命周期 ============

class TestWatchdogLifecycle:
    async def test_start_is_idempotent_and_stop_cancels(self, monkeypatch):
        monkeypatch.setattr(watchdog, "settings", SimpleNamespace(AGENT_TASK_WATCHDOG_INTERVAL=60))

        await watchdog.start_watchdog()
        first = watchdog._task
        assert first is not None and not first.done()

        await watchdog.start_watchdog()  # 幂等：不重复启动
        assert watchdog._task is first

        await watchdog.stop_watchdog()
        assert watchdog._task is None
        assert first.cancelled()

    async def test_disabled_when_interval_non_positive(self, monkeypatch):
        monkeypatch.setattr(watchdog, "settings", SimpleNamespace(AGENT_TASK_WATCHDOG_INTERVAL=0))

        await watchdog.start_watchdog()
        assert watchdog._task is None

    async def test_loop_survives_reconcile_errors(self, monkeypatch):
        """单轮对账异常不得终止常驻协程"""
        monkeypatch.setattr(watchdog, "watchdog_interval", lambda: 0.01)

        calls = {"n": 0}

        async def flaky_reconcile():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("数据库闪断")
            return {}

        monkeypatch.setattr(watchdog, "reconcile_once", flaky_reconcile)

        await watchdog.start_watchdog()
        try:
            await _sleep_until(lambda: calls["n"] >= 2, timeout=2)
        finally:
            await watchdog.stop_watchdog()

        assert calls["n"] >= 2  # 第一轮异常后仍执行了第二轮


async def _sleep_until(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.01)
