"""
Agent 注册表和动态Agent树管理

提供：
- Agent实例注册和管理
- 动态Agent树结构
- Agent状态追踪
- 子Agent创建和销毁
"""

import logging
import threading
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .state import AgentState

logger = logging.getLogger(__name__)


class AgentRegistry:
    """
    Agent 注册表
    
    管理所有Agent实例，维护动态Agent树结构
    """
    
    def __init__(self):
        self._lock = threading.RLock()
        
        # Agent图结构
        self._agent_graph: Dict[str, Any] = {
            "nodes": {},  # agent_id -> node_info
            "edges": [],  # {from, to, type}
        }
        
        # Agent实例和状态
        self._agent_instances: Dict[str, Any] = {}  # agent_id -> agent_instance
        self._agent_states: Dict[str, "AgentState"] = {}  # agent_id -> state
        
        # 消息队列
        self._agent_messages: Dict[str, List[Dict[str, Any]]] = {}  # agent_id -> messages
        
        # 根Agent
        self._root_agent_id: Optional[str] = None
        
        # 运行中的Agent线程
        self._running_agents: Dict[str, threading.Thread] = {}
    
    # ============ Agent 注册 ============
    
    def register_agent(
        self,
        agent_id: str,
        agent_name: str,
        agent_type: str,
        task: str,
        parent_id: Optional[str] = None,
        agent_instance: Any = None,
        state: Optional["AgentState"] = None,
        knowledge_modules: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        注册Agent到注册表
        
        Args:
            agent_id: Agent唯一标识
            agent_name: Agent名称
            agent_type: Agent类型
            task: 任务描述
            parent_id: 父Agent ID
            agent_instance: Agent实例
            state: Agent状态
            knowledge_modules: 加载的知识模块
            
        Returns:
            注册的节点信息
        """
        logger.debug(f"[AgentRegistry] register_agent 被调用: {agent_name} (id={agent_id}, parent={parent_id})")
        logger.debug(f"[AgentRegistry] 当前节点数: {len(self._agent_graph['nodes'])}, 节点列表: {list(self._agent_graph['nodes'].keys())}")
        
        with self._lock:
            node = {
                "id": agent_id,
                "name": agent_name,
                "type": agent_type,
                "task": task,
                "status": "running",
                "parent_id": parent_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "result": None,
                "knowledge_modules": knowledge_modules or [],
                "children": [],
            }
            
            self._agent_graph["nodes"][agent_id] = node
            
            if agent_instance:
                self._agent_instances[agent_id] = agent_instance
            
            if state:
                self._agent_states[agent_id] = state
            
            # 初始化消息队列
            if agent_id not in self._agent_messages:
                self._agent_messages[agent_id] = []
            
            # 添加边（父子关系）
            if parent_id:
                self._agent_graph["edges"].append({
                    "from": parent_id,
                    "to": agent_id,
                    "type": "delegation",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                
                # 更新父节点的children列表
                if parent_id in self._agent_graph["nodes"]:
                    self._agent_graph["nodes"][parent_id]["children"].append(agent_id)
            
            # 设置根Agent
            if parent_id is None and self._root_agent_id is None:
                self._root_agent_id = agent_id
            
            logger.debug(f"[AgentRegistry] 注册完成: {agent_name} ({agent_id}), parent: {parent_id}")
            logger.debug(f"[AgentRegistry] 注册后节点数: {len(self._agent_graph['nodes'])}, 节点列表: {list(self._agent_graph['nodes'].keys())}")
            return node
    
    def unregister_agent(self, agent_id: str) -> None:
        """注销Agent"""
        with self._lock:
            if agent_id in self._agent_graph["nodes"]:
                del self._agent_graph["nodes"][agent_id]
            
            self._agent_instances.pop(agent_id, None)
            self._agent_states.pop(agent_id, None)
            self._agent_messages.pop(agent_id, None)
            self._running_agents.pop(agent_id, None)
            
            # 移除相关边
            self._agent_graph["edges"] = [
                e for e in self._agent_graph["edges"]
                if e["from"] != agent_id and e["to"] != agent_id
            ]
            
            logger.debug(f"Unregistered agent: {agent_id}")
    
    # ============ Agent 状态更新 ============
    
    def update_agent_status(
        self,
        agent_id: str,
        status: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        """更新Agent状态"""
        with self._lock:
            if agent_id in self._agent_graph["nodes"]:
                node = self._agent_graph["nodes"][agent_id]
                node["status"] = status
                
                if status in ["completed", "failed", "stopped"]:
                    node["finished_at"] = datetime.now(timezone.utc).isoformat()
                
                if result:
                    node["result"] = result
                
                logger.debug(f"Updated agent {agent_id} status to {status}")
    
    def get_agent_status(self, agent_id: str) -> Optional[str]:
        """获取Agent状态"""
        with self._lock:
            if agent_id in self._agent_graph["nodes"]:
                return self._agent_graph["nodes"][agent_id]["status"]
            return None
    
    # ============ Agent 查询 ============
    
    def get_agent(self, agent_id: str) -> Optional[Any]:
        """获取Agent实例"""
        return self._agent_instances.get(agent_id)
    
    def get_agent_state(self, agent_id: str) -> Optional["AgentState"]:
        """获取Agent状态"""
        return self._agent_states.get(agent_id)
    
    def get_agent_node(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """获取Agent节点信息"""
        return self._agent_graph["nodes"].get(agent_id)
    
    def get_root_agent_id(self) -> Optional[str]:
        """获取根Agent ID"""
        return self._root_agent_id
    
    def get_children(self, agent_id: str) -> List[str]:
        """获取子Agent ID列表"""
        with self._lock:
            node = self._agent_graph["nodes"].get(agent_id)
            if node:
                return node.get("children", [])
            return []
    
    def get_parent(self, agent_id: str) -> Optional[str]:
        """获取父Agent ID"""
        with self._lock:
            node = self._agent_graph["nodes"].get(agent_id)
            if node:
                return node.get("parent_id")
            return None
    
    # ============ Agent 树操作 ============
    
    def get_agent_tree(self) -> Dict[str, Any]:
        """获取完整的Agent树结构"""
        with self._lock:
            return {
                "nodes": dict(self._agent_graph["nodes"]),
                "edges": list(self._agent_graph["edges"]),
                "root_agent_id": self._root_agent_id,
            }
    
    def get_agent_tree_view(self, agent_id: Optional[str] = None) -> str:
        """获取Agent树的文本视图"""
        with self._lock:
            lines = ["=== AGENT TREE ==="]
            
            root_id = agent_id or self._root_agent_id
            if not root_id or root_id not in self._agent_graph["nodes"]:
                return "No agents in the tree"
            
            def _build_tree(aid: str, depth: int = 0) -> None:
                node = self._agent_graph["nodes"].get(aid)
                if not node:
                    return
                
                indent = "  " * depth
                status_emoji = {
                    "running": "🔄",
                    "waiting": "⏳",
                    "completed": "✅",
                    "failed": "❌",
                    "stopped": "🛑",
                }.get(node["status"], "❓")
                
                lines.append(f"{indent}{status_emoji} {node['name']} ({aid})")
                lines.append(f"{indent}   Task: {node['task'][:50]}...")
                lines.append(f"{indent}   Status: {node['status']}")
                
                if node.get("knowledge_modules"):
                    lines.append(f"{indent}   Modules: {', '.join(node['knowledge_modules'])}")
                
                for child_id in node.get("children", []):
                    _build_tree(child_id, depth + 1)
            
            _build_tree(root_id)
            return "\n".join(lines)
    
    def get_statistics(self) -> Dict[str, int]:
        """获取统计信息"""
        with self._lock:
            stats = {
                "total": len(self._agent_graph["nodes"]),
                "running": 0,
                "waiting": 0,
                "completed": 0,
                "failed": 0,
                "stopped": 0,
            }
            
            for node in self._agent_graph["nodes"].values():
                status = node.get("status", "unknown")
                if status in stats:
                    stats[status] += 1
            
            return stats
    
    # ============ 清理 ============
    
    def clear(self) -> None:
        """清空注册表"""
        with self._lock:
            self._agent_graph = {"nodes": {}, "edges": []}
            self._agent_instances.clear()
            self._agent_states.clear()
            self._agent_messages.clear()
            self._running_agents.clear()
            self._root_agent_id = None
            logger.debug("Agent registry cleared")
    
    def cleanup_finished_agents(self) -> int:
        """清理已完成的Agent"""
        with self._lock:
            finished_ids = [
                aid for aid, node in self._agent_graph["nodes"].items()
                if node["status"] in ["completed", "failed", "stopped"]
            ]
            
            for aid in finished_ids:
                # 保留节点信息，但清理实例
                self._agent_instances.pop(aid, None)
                self._running_agents.pop(aid, None)
            
            return len(finished_ids)


# 全局注册表实例（向后兼容：未绑定任务级注册表时的回退）
agent_registry = AgentRegistry()


# ============ 任务级注册表（支持多审计任务并发） ============
#
# 背景：Agent 注册表原本是进程级全局单例，多个审计任务并发执行时会互相
# 清空/覆盖 Agent 树（clear()、单一 root_agent_id、stop_all_agents() 等），
# 导致任务无法 finish、取消一个任务连带杀掉其他任务。
#
# 方案：每个审计任务绑定独立的 AgentRegistry 实例，通过 ContextVar 传播。
# asyncio.create_task / asyncio.to_thread 都会复制当前 Context，因此子 Agent、
# 工具调用、线程池内的阻塞操作都能自动继承本任务的注册表。

_current_registry: ContextVar[Optional[AgentRegistry]] = ContextVar(
    "deepaudit_agent_registry", default=None
)

# 审计任务 ID -> 该任务专属注册表（供 API 层按 task_id 定位，如取消/查询 Agent 树）
_task_registries: Dict[str, AgentRegistry] = {}
_registries_lock = threading.RLock()


def get_agent_registry() -> AgentRegistry:
    """获取当前执行上下文的 Agent 注册表

    未绑定任务级注册表时回退到全局单例，保证既有单任务行为不变。
    """
    registry = _current_registry.get()
    if registry is None:
        return agent_registry
    return registry


def bind_task_registry(audit_task_id: str) -> AgentRegistry:
    """为审计任务创建并绑定独立注册表

    必须在任务协程的最开始调用（此时 ContextVar 的设置会被后续创建的
    所有子任务继承）。

    Args:
        audit_task_id: 审计任务 ID（AgentTask.id）

    Returns:
        该任务专属的注册表实例
    """
    registry = AgentRegistry()

    with _registries_lock:
        old = _task_registries.get(audit_task_id)
        _task_registries[audit_task_id] = registry

    _current_registry.set(registry)

    if old is not None:
        logger.warning(
            f"[AgentRegistry] Task {audit_task_id} rebound a new registry, "
            f"discarding {len(old.get_agent_tree()['nodes'])} old nodes"
        )

    logger.debug(f"[AgentRegistry] Bound task registry for {audit_task_id}")
    return registry


def get_task_registry(audit_task_id: str) -> Optional[AgentRegistry]:
    """按审计任务 ID 获取注册表（供 API 层使用，不依赖调用上下文）"""
    with _registries_lock:
        return _task_registries.get(audit_task_id)


def unbind_task_registry(audit_task_id: str) -> None:
    """解绑并销毁审计任务的注册表（任务结束时调用）"""
    with _registries_lock:
        registry = _task_registries.pop(audit_task_id, None)

    if registry is not None:
        registry.clear()

    # 仅当当前上下文绑定的正是该任务的注册表时才重置，
    # 避免误清其他任务在同一上下文中设置的值
    if _current_registry.get() is registry:
        _current_registry.set(None)

    logger.debug(f"[AgentRegistry] Unbound task registry for {audit_task_id}")


def list_active_task_registries() -> List[str]:
    """列出当前绑定了注册表的审计任务 ID（用于监控/调试）"""
    with _registries_lock:
        return list(_task_registries.keys())


__all__ = [
    "AgentRegistry",
    "agent_registry",
    "get_agent_registry",
    "bind_task_registry",
    "get_task_registry",
    "unbind_task_registry",
    "list_active_task_registries",
]
