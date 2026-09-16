"""
批量审计执行引擎测试

环境未安装 aiosqlite，无法起真实异步库，因此：
- 并发/完整性/容错/取消语义：把 batch_runner 的 DB 辅助函数与 `_execute_agent_task`
  替换为内存记录器，直接驱动 `_execute_one`
- cancel_batch_run / _finalize_run：用按语句类型分发的 FakeSession 校验落库行为

对应方案 2.7 的四条验收断言：
① 并发峰值恰好等于 concurrency
② 1000 个项目全部被处理且无遗漏
③ 单项目抛异常不影响其余项目
④ 取消后未出队项目标记 cancelled
"""

import asyncio
from collections import defaultdict
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy.sql import Select
from sqlalchemy.sql.dml import Update

from app.models.agent_task import AgentTaskStatus
from app.models.batch_audit import BatchRunItemStatus, BatchRunStatus
from app.services.scheduler import batch_runner


RUN_ID = "run-test-0001"
CREATED_BY = "user-1"


@pytest.fixture(autouse=True)
def _reset_module_state():
    """每个测试前后清理模块级取消集合与批次登记表"""
    batch_runner._cancelled_runs.clear()
    batch_runner._running_batches.clear()
    yield
    batch_runner._cancelled_runs.clear()
    batch_runner._running_batches.clear()


def _make_item(index: int) -> batch_runner._ItemSnapshot:
    return batch_runner._ItemSnapshot(
        id=f"item-{index}",
        project_id=f"proj-{index}",
        project_name=f"project-{index}",
    )


class Recorder:
    """内存记录器：接管 batch_runner 的所有 DB 写入与 Agent 执行"""

    def __init__(self, *, delay: float = 0.005, failing: Optional[set] = None):
        self.delay = delay
        self.failing = failing or set()

        self.statuses: Dict[str, List[str]] = defaultdict(list)
        self.final_status: Dict[str, str] = {}
        self.findings: Dict[str, int] = {}
        self.errors: Dict[str, str] = {}
        self.counters: Dict[str, int] = defaultdict(int)

        self.created_tasks: List[str] = []
        self.executed: List[str] = []

        self.current = 0
        self.peak = 0

    # ---- 替换 batch_runner 的 DB 辅助函数 ----

    async def set_item_status(
        self,
        item_id: str,
        status: str,
        *,
        agent_task_id: Optional[str] = None,
        findings_count: Optional[int] = None,
        error_message: Optional[str] = None,
        started: bool = False,
        finished: bool = False,
    ) -> None:
        self.statuses[item_id].append(status)
        self.final_status[item_id] = status
        if findings_count is not None:
            self.findings[item_id] = findings_count
        if error_message is not None:
            self.errors[item_id] = error_message

    async def apply_counter_delta(self, run_id: str, **deltas: int) -> None:
        for field, delta in deltas.items():
            self.counters[field] += delta

    async def create_agent_task(
        self, project_id: str, project_name: str, run_id: str,
        template: Dict[str, Any], created_by: str,
    ) -> Dict[str, Any]:
        await asyncio.sleep(0)
        task_id = f"task-{project_id}"
        self.created_tasks.append(task_id)
        return {"id": task_id}

    async def read_agent_task_result(self, agent_task_id: str) -> Dict[str, Any]:
        index = agent_task_id.rsplit("-", 1)[-1]
        if index in self.failing:
            return {
                "status": AgentTaskStatus.FAILED,
                "findings_count": 0,
                "error_message": "模拟审计失败",
            }
        return {
            "status": AgentTaskStatus.COMPLETED,
            "findings_count": 3,
            "error_message": None,
        }

    # ---- 替换 _execute_agent_task ----

    async def execute_agent_task(self, task_id: str) -> None:
        self.current += 1
        self.peak = max(self.peak, self.current)
        try:
            await asyncio.sleep(self.delay)
            index = task_id.rsplit("-", 1)[-1]
            if index in self.failing:
                raise RuntimeError(f"模拟项目 {index} 崩溃")
            self.executed.append(task_id)
        finally:
            self.current -= 1

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(batch_runner, "_set_item_status", self.set_item_status)
        monkeypatch.setattr(batch_runner, "_apply_counter_delta", self.apply_counter_delta)
        monkeypatch.setattr(batch_runner, "_create_agent_task", self.create_agent_task)
        monkeypatch.setattr(
            batch_runner, "_read_agent_task_result", self.read_agent_task_result
        )
        # `_execute_one` 内部是延迟 import，patch 模块属性即可生效
        monkeypatch.setattr(
            "app.api.v1.endpoints.agent_tasks._execute_agent_task",
            self.execute_agent_task,
        )


async def _drive(items: List[batch_runner._ItemSnapshot], concurrency: int) -> None:
    """按 run_batch 的并发模型驱动一批明细"""
    semaphore = asyncio.Semaphore(concurrency)
    await asyncio.gather(
        *[
            batch_runner._execute_one(semaphore, RUN_ID, item, {}, CREATED_BY)
            for item in items
        ],
        return_exceptions=True,
    )


# ============ ① 并发峰值恰好等于 concurrency ============

class TestConcurrency:
    async def test_peak_equals_concurrency(self, monkeypatch):
        recorder = Recorder(delay=0.02)
        recorder.install(monkeypatch)

        items = [_make_item(i) for i in range(40)]
        await _drive(items, concurrency=5)

        assert recorder.peak == 5
        assert len(recorder.executed) == 40

    @pytest.mark.parametrize("concurrency", [1, 2, 5, 10])
    async def test_peak_matches_configured_value(self, monkeypatch, concurrency):
        recorder = Recorder(delay=0.02)
        recorder.install(monkeypatch)

        items = [_make_item(i) for i in range(concurrency * 4)]
        await _drive(items, concurrency=concurrency)

        assert recorder.peak == concurrency
        assert len(recorder.executed) == concurrency * 4

    async def test_counters_stay_consistent(self, monkeypatch):
        recorder = Recorder(delay=0.001)
        recorder.install(monkeypatch)

        items = [_make_item(i) for i in range(20)]
        await _drive(items, concurrency=4)

        assert recorder.counters["queued_count"] == -20
        assert recorder.counters["running_count"] == 0  # +20 出队 / -20 收尾
        assert recorder.counters["completed_count"] == 20
        assert recorder.counters["failed_count"] == 0


# ============ ② 1000 个项目全部被处理且无遗漏 ============

class TestScale:
    async def test_thousand_projects_all_processed(self, monkeypatch):
        recorder = Recorder(delay=0)
        recorder.install(monkeypatch)

        items = [_make_item(i) for i in range(1000)]
        await _drive(items, concurrency=5)

        # 无遗漏、无重复
        assert len(recorder.executed) == 1000
        assert len(set(recorder.executed)) == 1000
        assert set(recorder.executed) == {f"task-proj-{i}" for i in range(1000)}

        # 每个明细都走到了终态 completed
        assert len(recorder.final_status) == 1000
        assert all(s == BatchRunItemStatus.COMPLETED for s in recorder.final_status.values())
        assert recorder.counters["completed_count"] == 1000

    async def test_item_goes_through_running_then_completed(self, monkeypatch):
        recorder = Recorder(delay=0)
        recorder.install(monkeypatch)

        await _drive([_make_item(0)], concurrency=1)

        assert recorder.statuses["item-0"] == [
            BatchRunItemStatus.RUNNING,   # 出队
            BatchRunItemStatus.RUNNING,   # 回写 agent_task_id
            BatchRunItemStatus.COMPLETED,
        ]
        assert recorder.findings["item-0"] == 3


# ============ ③ 单项目抛异常不影响其余 ============

class TestFaultIsolation:
    async def test_exception_in_one_project_does_not_stop_others(self, monkeypatch):
        recorder = Recorder(delay=0.001, failing={"7", "42", "99"})
        recorder.install(monkeypatch)

        items = [_make_item(i) for i in range(120)]
        await _drive(items, concurrency=5)

        # 抛异常的 3 个不会进入 executed，其余 117 个照常完成
        assert len(recorder.executed) == 117
        assert len(recorder.final_status) == 120

        failed_items = [
            item_id for item_id, s in recorder.final_status.items()
            if s == BatchRunItemStatus.FAILED
        ]
        assert len(failed_items) == 3
        assert recorder.counters["failed_count"] == 3
        assert recorder.counters["completed_count"] == 117
        assert recorder.counters["running_count"] == 0
        # 异常信息被记录下来
        assert all("崩溃" in recorder.errors[i] for i in failed_items)

    async def test_task_reported_failed_is_marked_failed(self, monkeypatch):
        """Agent 任务正常返回但状态为 failed → 明细记 failed 且带错误信息"""
        recorder = Recorder(delay=0, failing=set())
        recorder.install(monkeypatch)

        async def fake_execute(task_id: str) -> None:
            recorder.current += 1
            recorder.peak = max(recorder.peak, recorder.current)
            recorder.current -= 1

        async def fake_result(agent_task_id: str) -> Dict[str, Any]:
            index = agent_task_id.rsplit("-", 1)[-1]
            if index == "3":
                return {
                    "status": AgentTaskStatus.FAILED,
                    "findings_count": 0,
                    "error_message": "沙箱超时",
                }
            return {
                "status": AgentTaskStatus.COMPLETED,
                "findings_count": 1,
                "error_message": None,
            }

        monkeypatch.setattr(
            "app.api.v1.endpoints.agent_tasks._execute_agent_task", fake_execute
        )
        monkeypatch.setattr(batch_runner, "_read_agent_task_result", fake_result)

        await _drive([_make_item(i) for i in range(10)], concurrency=3)

        assert recorder.final_status["item-3"] == BatchRunItemStatus.FAILED
        assert recorder.errors["item-3"] == "沙箱超时"
        assert recorder.counters["failed_count"] == 1
        assert recorder.counters["completed_count"] == 9

    async def test_cancelled_agent_task_maps_to_cancelled_item(self, monkeypatch):
        recorder = Recorder(delay=0)
        recorder.install(monkeypatch)

        async def fake_result(agent_task_id: str) -> Dict[str, Any]:
            return {
                "status": AgentTaskStatus.CANCELLED,
                "findings_count": 0,
                "error_message": "用户取消",
            }

        monkeypatch.setattr(batch_runner, "_read_agent_task_result", fake_result)

        await _drive([_make_item(i) for i in range(4)], concurrency=2)

        assert all(
            s == BatchRunItemStatus.CANCELLED for s in recorder.final_status.values()
        )
        assert recorder.counters["skipped_count"] == 4
        assert recorder.counters["completed_count"] == 0


# ============ ④ 取消后未出队项目标记 cancelled ============

class TestCancellation:
    async def test_cancelled_run_skips_queued_items(self, monkeypatch):
        """批次已取消 → 出队的项目不创建 AgentTask，直接标 cancelled"""
        recorder = Recorder(delay=0)
        recorder.install(monkeypatch)

        batch_runner._cancelled_runs.add(RUN_ID)
        await _drive([_make_item(i) for i in range(50)], concurrency=5)

        assert recorder.created_tasks == []
        assert recorder.executed == []
        assert len(recorder.final_status) == 50
        assert all(
            s == BatchRunItemStatus.CANCELLED for s in recorder.final_status.values()
        )
        assert recorder.counters["queued_count"] == -50
        assert recorder.counters["skipped_count"] == 50
        assert recorder.counters["running_count"] == 0

    async def test_cancel_takes_effect_midway(self, monkeypatch):
        """跑到一半请求取消：已出队的正常收尾，剩余未出队的标 cancelled"""
        recorder = Recorder(delay=0.01)
        recorder.install(monkeypatch)

        items = [_make_item(i) for i in range(30)]
        semaphore = asyncio.Semaphore(2)

        async def cancel_later():
            await asyncio.sleep(0.05)
            batch_runner._cancelled_runs.add(RUN_ID)

        await asyncio.gather(
            *[
                batch_runner._execute_one(semaphore, RUN_ID, item, {}, CREATED_BY)
                for item in items
            ] + [cancel_later()],
            return_exceptions=True,
        )

        statuses = list(recorder.final_status.values())
        assert BatchRunItemStatus.COMPLETED in statuses
        assert BatchRunItemStatus.CANCELLED in statuses
        assert len(statuses) == 30
        # 已出队完成的不受取消影响
        assert len(recorder.executed) == statuses.count(BatchRunItemStatus.COMPLETED)

    async def test_is_run_cancelled_helper(self):
        assert batch_runner.is_run_cancelled(RUN_ID) is False
        batch_runner._cancelled_runs.add(RUN_ID)
        assert batch_runner.is_run_cancelled(RUN_ID) is True


# ============ FakeSession：校验 cancel_batch_run / _finalize_run 的落库 ============

class FakeResult:
    def __init__(self, rows: Optional[List[Any]] = None, rowcount: int = 0):
        self._rows = rows or []
        self.rowcount = rowcount

    def scalars(self) -> "FakeResult":
        return self

    def all(self) -> List[Any]:
        return self._rows


class FakeSession:
    """按语句类型分发的最小 AsyncSession 替身"""

    def __init__(self, handler):
        self._handler = handler
        self.committed = 0
        self.statements: List[Any] = []

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def execute(self, stmt, *args, **kwargs) -> FakeResult:
        self.statements.append(stmt)
        return self._handler(stmt)

    async def get(self, model, pk):
        return self._handler(("get", model, pk))

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        return None


def _install_session(monkeypatch, handler) -> List[FakeSession]:
    sessions: List[FakeSession] = []

    def factory():
        session = FakeSession(handler)
        sessions.append(session)
        return session

    monkeypatch.setattr(batch_runner, "async_session_factory", factory)
    return sessions


class TestCancelBatchRun:
    async def test_cancels_running_tasks_and_queued_items(self, monkeypatch):
        running_items = [
            SimpleNamespace(id="item-a", agent_task_id="task-a"),
            SimpleNamespace(id="item-b", agent_task_id="task-b"),
            SimpleNamespace(id="item-c", agent_task_id=None),
        ]
        queued_rowcount = 997
        updates: List[Any] = []
        requested: List[str] = []

        monkeypatch.setattr(
            "app.api.v1.endpoints.agent_tasks.request_agent_task_cancellation",
            requested.append,
        )

        def handler(stmt):
            if isinstance(stmt, Select):
                return FakeResult(rows=running_items)
            if isinstance(stmt, Update):
                updates.append(stmt)
                # 第一条 Update 针对明细，返回被取消的排队条数
                if stmt.table.name == "audit_batch_run_items":
                    return FakeResult(rowcount=queued_rowcount)
                return FakeResult(rowcount=1)
            raise AssertionError(f"未预期的语句: {stmt}")

        sessions = _install_session(monkeypatch, handler)

        # 注册一个已结束的批次协程：只有本进程真正持有协程的批次才会置取消标志
        idle_task = asyncio.create_task(asyncio.sleep(0))
        await idle_task
        batch_runner.register_batch_task(RUN_ID, idle_task)

        result = await batch_runner.cancel_batch_run(RUN_ID)

        assert result == {
            "run_id": RUN_ID,
            "interrupted_tasks": 2,          # item-c 没有 agent_task_id
            "cancelled_queued": queued_rowcount,
        }
        assert requested == ["task-a", "task-b"]
        assert batch_runner.is_run_cancelled(RUN_ID) is True

        # 明细批量置 cancelled + 批次计数同步各一条 Update
        assert len(updates) == 2
        assert sessions[0].committed == 1

    async def test_cancels_registered_batch_task(self, monkeypatch):
        def handler(stmt):
            if isinstance(stmt, Select):
                return FakeResult(rows=[])
            if isinstance(stmt, Update):
                return FakeResult(rowcount=0)
            raise AssertionError(f"未预期的语句: {stmt}")

        _install_session(monkeypatch, handler)

        started = asyncio.Event()

        async def long_running():
            started.set()
            await asyncio.sleep(30)

        task = asyncio.create_task(long_running())
        await started.wait()
        batch_runner.register_batch_task(RUN_ID, task)

        await batch_runner.cancel_batch_run(RUN_ID)

        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

    async def test_unregistered_run_does_not_leak_cancel_flag(self, monkeypatch):
        """本进程没有在跑协程时不得置取消标志

        `_cancelled_runs` 的唯一清理点在 `run_batch` 的 finally，对已结束的批次
        置标志会让条目永久残留在内存里；但 DB 清理仍须执行，以修正残留明细。
        """
        updates: List[Any] = []

        def handler(stmt):
            if isinstance(stmt, Select):
                return FakeResult(rows=[])
            if isinstance(stmt, Update):
                updates.append(stmt)
                return FakeResult(rowcount=3)
            raise AssertionError(f"未预期的语句: {stmt}")

        sessions = _install_session(monkeypatch, handler)

        result = await batch_runner.cancel_batch_run(RUN_ID)

        assert batch_runner.is_run_cancelled(RUN_ID) is False
        assert batch_runner._cancelled_runs == set()
        # DB 侧清理照常执行
        assert result["cancelled_queued"] == 3
        assert len(updates) == 2
        assert sessions[0].committed == 1


class TestFinalizeRun:
    async def _finalize(self, monkeypatch, item_counts: Dict[str, int], **kwargs):
        run = SimpleNamespace(
            id=RUN_ID, status=BatchRunStatus.RUNNING, queued_count=0, running_count=0,
            completed_count=0, failed_count=0, skipped_count=7, total_projects=10,
            finished_at=None, error_message=None,
        )
        rows = [(status, count) for status, count in item_counts.items()]

        def handler(stmt):
            if isinstance(stmt, tuple) and stmt[0] == "get":
                return run
            if isinstance(stmt, Select):
                return FakeResult(rows=rows)
            raise AssertionError(f"未预期的语句: {stmt}")

        _install_session(monkeypatch, handler)
        await batch_runner._finalize_run(RUN_ID, **kwargs)
        return run

    async def test_all_completed(self, monkeypatch):
        run = await self._finalize(
            monkeypatch, {BatchRunItemStatus.COMPLETED: 10}
        )
        assert run.status == BatchRunStatus.COMPLETED
        assert run.completed_count == 10
        assert run.failed_count == 0
        assert run.finished_at is not None

    async def test_partial_failure(self, monkeypatch):
        run = await self._finalize(
            monkeypatch,
            {BatchRunItemStatus.COMPLETED: 8, BatchRunItemStatus.FAILED: 2},
        )
        assert run.status == BatchRunStatus.PARTIALLY_FAILED
        assert run.completed_count == 8
        assert run.failed_count == 2

    async def test_all_failed(self, monkeypatch):
        run = await self._finalize(
            monkeypatch, {BatchRunItemStatus.FAILED: 5}
        )
        assert run.status == BatchRunStatus.FAILED
        assert run.completed_count == 0
        assert run.failed_count == 5

    async def test_cancelled_items_make_run_partially_failed(self, monkeypatch):
        run = await self._finalize(
            monkeypatch,
            {BatchRunItemStatus.COMPLETED: 3, BatchRunItemStatus.CANCELLED: 7},
        )
        assert run.status == BatchRunStatus.PARTIALLY_FAILED

    async def test_explicit_status_and_error_override(self, monkeypatch):
        run = await self._finalize(
            monkeypatch,
            {BatchRunItemStatus.COMPLETED: 10},
            status=BatchRunStatus.CANCELLED,
            error_message="批次被取消",
        )
        assert run.status == BatchRunStatus.CANCELLED
        assert run.error_message == "批次被取消"

    async def test_skipped_count_reconciled_with_items(self, monkeypatch):
        """total_projects 不被改写；skipped_count 按明细重算并保留无明细行的基线部分"""
        run = await self._finalize(
            monkeypatch,
            {BatchRunItemStatus.COMPLETED: 1, BatchRunItemStatus.SKIPPED: 2},
        )
        assert run.total_projects == 10   # 启动时写入的值未被改写
        # 3 条明细 -> 基线 skipped = 10 - 3 = 7（已删除/非本人项目，无明细行），
        # 再叠加明细里的 2 条 SKIPPED
        assert run.skipped_count == 9
        # 收尾后分项之和必须恒等于总数，否则前端进度条会超过 100%
        assert (
            run.queued_count + run.running_count + run.completed_count
            + run.failed_count + run.skipped_count
        ) == run.total_projects

    async def test_negative_incremental_counters_are_corrected(self, monkeypatch):
        """运行期增量计数被取消路径双计成负数时，收尾按明细校正回来"""
        run = SimpleNamespace(
            id=RUN_ID, status=BatchRunStatus.RUNNING,
            queued_count=-3, running_count=0, completed_count=99, failed_count=0,
            skipped_count=99, total_projects=10, finished_at=None, error_message=None,
        )
        rows = [
            (BatchRunItemStatus.COMPLETED, 2),
            (BatchRunItemStatus.CANCELLED, 8),
        ]

        def handler(stmt):
            if isinstance(stmt, tuple) and stmt[0] == "get":
                return run
            if isinstance(stmt, Select):
                return FakeResult(rows=rows)
            raise AssertionError(f"未预期的语句: {stmt}")

        _install_session(monkeypatch, handler)
        await batch_runner._finalize_run(RUN_ID)

        assert run.queued_count == 0        # 不会把 -3 原样返给前端
        assert run.running_count == 0
        assert run.completed_count == 2     # 脏值 99 被明细真实值覆盖
        assert run.skipped_count == 8       # 明细全为终态 -> 基线 0 + 8 条 CANCELLED
        assert run.total_projects == 10


class TestRunBatchWrapper:
    async def test_run_batch_swallows_crash_and_marks_failed(self, monkeypatch):
        finalized: List[Dict[str, Any]] = []

        async def crash(run_id: str) -> None:
            raise RuntimeError("数据库炸了")

        async def fake_finalize(run_id, status=None, error_message=None):
            finalized.append({"status": status, "error": error_message})

        monkeypatch.setattr(batch_runner, "_run_batch_inner", crash)
        monkeypatch.setattr(batch_runner, "_finalize_run", fake_finalize)

        await batch_runner.run_batch(RUN_ID)  # 不应向上抛

        assert finalized == [{"status": BatchRunStatus.FAILED, "error": "数据库炸了"}]

    async def test_run_batch_cleans_up_state(self, monkeypatch):
        async def noop(run_id: str) -> None:
            return None

        async def fake_finalize(run_id, status=None, error_message=None):
            return None

        monkeypatch.setattr(batch_runner, "_run_batch_inner", noop)
        monkeypatch.setattr(batch_runner, "_finalize_run", fake_finalize)

        batch_runner._cancelled_runs.add(RUN_ID)
        sentinel = asyncio.get_running_loop().create_future()
        sentinel.set_result(None)
        batch_runner._running_batches[RUN_ID] = sentinel

        await batch_runner.run_batch(RUN_ID)

        assert RUN_ID not in batch_runner._cancelled_runs
        assert RUN_ID not in batch_runner._running_batches


class TestStartBatchRun:
    async def test_registers_task_and_is_cancellable(self, monkeypatch):
        started = asyncio.Event()

        async def fake_run(run_id: str) -> None:
            started.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(batch_runner, "run_batch", fake_run)

        task = batch_runner.start_batch_run(RUN_ID)
        assert batch_runner.list_running_batch_ids() == [RUN_ID]

        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
