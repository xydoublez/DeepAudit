"""
任务级 Agent 注册表隔离测试

背景：Agent 注册表原本是进程级全局单例，多个审计任务并发执行时会互相清空/
覆盖 Agent 树，导致任务无法 finish、取消一个任务连带杀掉其他任务。
本文件验证改造后（ContextVar + 按审计任务 ID 索引）：

1. 未绑定任务时回退全局单例（既有单任务行为不变）
2. 并发的两个审计任务各自持有独立注册表，节点互不可见
3. asyncio.create_task / asyncio.to_thread 能继承本任务的注册表
4. unbind 后回退全局单例、注册表从映射中移除
5. stop_all_agents 只作用于传入的 registry
"""

import asyncio

import pytest

from app.services.agent.core import registry as registry_module
from app.services.agent.core.registry import (
    AgentRegistry,
    agent_registry,
    bind_task_registry,
    get_agent_registry,
    get_task_registry,
    list_active_task_registries,
    unbind_task_registry,
)
from app.services.agent.core.graph_controller import stop_all_agents


@pytest.fixture(autouse=True)
def _isolate_registries():
    """每个测试前后清理全局单例与任务级注册表，避免相互污染"""
    yield
    for task_id in list_active_task_registries():
        unbind_task_registry(task_id)
    registry_module._current_registry.set(None)
    agent_registry.clear()


def _register_agent(
    reg: AgentRegistry,
    agent_id: str,
    parent_id: str = None,
    name: str = None,
) -> None:
    reg.register_agent(
        agent_id=agent_id,
        agent_name=name or agent_id,
        agent_type="orchestrator" if parent_id is None else "analysis",
        task=f"task for {agent_id}",
        parent_id=parent_id,
    )


# ============ 1. 回退行为 ============

def test_fallback_to_global_singleton_when_unbound():
    """未绑定任务级注册表时，get_agent_registry 返回全局单例"""
    assert get_agent_registry() is agent_registry


def test_get_task_registry_returns_none_for_unknown_task():
    assert get_task_registry("not-exist") is None


# ============ 2. 绑定与隔离 ============

def test_bind_creates_new_isolated_registry():
    reg = bind_task_registry("task-1")

    assert isinstance(reg, AgentRegistry)
    assert reg is not agent_registry
    assert get_task_registry("task-1") is reg
    assert get_agent_registry() is reg
    assert "task-1" in list_active_task_registries()


def test_bound_registry_is_not_visible_to_global_singleton():
    reg = bind_task_registry("task-1")
    _register_agent(reg, "orchestrator-1")

    assert "orchestrator-1" in reg.get_agent_tree()["nodes"]
    assert "orchestrator-1" not in agent_registry.get_agent_tree()["nodes"]


@pytest.mark.asyncio
async def test_concurrent_tasks_have_disjoint_agent_trees():
    """两个审计任务并发执行时，各自的 Agent 树互不可见"""
    seen = {}

    async def run_audit(task_id: str, root_id: str, child_id: str):
        reg = bind_task_registry(task_id)
        _register_agent(reg, root_id)
        _register_agent(reg, child_id, parent_id=root_id)
        # 让出控制权，制造真实的交错执行
        await asyncio.sleep(0)
        tree = get_agent_registry().get_agent_tree()
        seen[task_id] = {
            "nodes": set(tree["nodes"].keys()),
            "root": tree.get("root_agent_id"),
        }
        await asyncio.sleep(0)
        # 再次确认：其他任务的注册动作不会污染本任务
        assert set(get_agent_registry().get_agent_tree()["nodes"].keys()) == {root_id, child_id}

    await asyncio.gather(
        run_audit("task-A", "root-A", "child-A"),
        run_audit("task-B", "root-B", "child-B"),
    )

    assert seen["task-A"]["nodes"] == {"root-A", "child-A"}
    assert seen["task-B"]["nodes"] == {"root-B", "child-B"}
    # 每个任务都能正确识别自己的根 Agent（finish_tool 的 _validate_root_agent 依赖此行为）
    assert seen["task-A"]["root"] == "root-A"
    assert seen["task-B"]["root"] == "root-B"


@pytest.mark.asyncio
async def test_concurrent_bind_does_not_overwrite_sibling_context():
    """子协程内 bind 不会改变父协程绑定的注册表"""
    parent_reg = bind_task_registry("task-parent")

    async def child():
        child_reg = bind_task_registry("task-child")
        assert get_agent_registry() is child_reg
        return child_reg

    child_reg = await asyncio.create_task(child())

    assert child_reg is not parent_reg
    # 父协程的 ContextVar 未被子协程改写
    assert get_agent_registry() is parent_reg


# ============ 3. Context 传播 ============

@pytest.mark.asyncio
async def test_child_asyncio_task_inherits_registry():
    """asyncio.create_task 复制当前 Context，子任务继承注册表"""
    reg = bind_task_registry("task-1")
    _register_agent(reg, "orchestrator-1")

    async def sub_agent():
        return get_agent_registry()

    inherited = await asyncio.create_task(sub_agent())

    assert inherited is reg
    assert "orchestrator-1" in inherited.get_agent_tree()["nodes"]


@pytest.mark.asyncio
async def test_to_thread_inherits_registry():
    """asyncio.to_thread 同样复制 Context，线程内阻塞操作继承注册表"""
    reg = bind_task_registry("task-1")
    _register_agent(reg, "orchestrator-1")

    inherited = await asyncio.to_thread(get_agent_registry)

    assert inherited is reg


# ============ 4. 解绑 ============

def test_unbind_clears_registry_and_falls_back_to_global():
    reg = bind_task_registry("task-1")
    _register_agent(reg, "orchestrator-1")

    unbind_task_registry("task-1")

    assert get_task_registry("task-1") is None
    assert "task-1" not in list_active_task_registries()
    assert get_agent_registry() is agent_registry
    # 任务注册表被清空，不再持有 Agent 实例
    assert reg.get_agent_tree()["nodes"] == {}


def test_unbind_does_not_touch_other_task_registry():
    reg_a = bind_task_registry("task-A")
    reg_b = bind_task_registry("task-B")
    _register_agent(reg_a, "root-A")
    _register_agent(reg_b, "root-B")

    unbind_task_registry("task-A")

    assert get_task_registry("task-B") is reg_b
    assert "root-B" in reg_b.get_agent_tree()["nodes"]


def test_unbind_unknown_task_is_noop():
    unbind_task_registry("never-bound")
    assert get_agent_registry() is agent_registry


def test_rebind_replaces_previous_registry():
    first = bind_task_registry("task-1")
    second = bind_task_registry("task-1")

    assert first is not second
    assert get_task_registry("task-1") is second
    assert get_agent_registry() is second


# ============ 5. 作用域化的图控制 ============

def test_stop_all_agents_only_affects_scoped_registry():
    """取消某个任务时不得连带停止其他任务的 Agent"""
    reg_a = bind_task_registry("task-A")
    _register_agent(reg_a, "root-A")
    _register_agent(reg_a, "child-A", parent_id="root-A")

    reg_b = bind_task_registry("task-B")
    _register_agent(reg_b, "root-B")
    _register_agent(reg_b, "child-B", parent_id="root-B")

    result = stop_all_agents(exclude_root=False, registry=reg_a)

    assert result["success"] is True
    assert set(result["stopped"]) == {"root-A", "child-A"}

    tree_a = reg_a.get_agent_tree()["nodes"]
    tree_b = reg_b.get_agent_tree()["nodes"]
    assert tree_a["root-A"]["status"] == "stopping"
    assert tree_a["child-A"]["status"] == "stopping"
    # 其他任务的 Agent 完全未受影响
    assert tree_b["root-B"]["status"] == "running"
    assert tree_b["child-B"]["status"] == "running"


def test_stop_all_agents_excludes_root_by_default():
    reg = bind_task_registry("task-A")
    _register_agent(reg, "root-A")
    _register_agent(reg, "child-A", parent_id="root-A")

    result = stop_all_agents(registry=reg)

    assert result["stopped"] == ["child-A"]
    assert reg.get_agent_tree()["nodes"]["root-A"]["status"] == "running"
