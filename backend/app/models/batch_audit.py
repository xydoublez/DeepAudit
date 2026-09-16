"""
定时批量审计模型

支持「一个计划 → 定时触发 → 批量并发审计 N 个项目」的能力：

- AuditSchedule:      定时计划（项目范围 / 并发度 / 调度方式 / 审计参数模板）
- AuditBatchRun:      一次批次执行记录（含实时进度计数）
- AuditBatchRunItem:  批次内单个项目的执行明细（关联到具体的 AgentTask）
"""

import uuid

from sqlalchemy import (
    Column, String, Integer, Text, Boolean,
    DateTime, ForeignKey, JSON, Index,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base


class ScheduleType:
    """调度方式"""
    ONCE = "once"        # 一次性（到点执行一次后自动停用）
    DAILY = "daily"      # 每天固定时间
    WEEKLY = "weekly"    # 每周指定星期 + 固定时间
    INTERVAL = "interval"  # 固定小时间隔
    MANUAL = "manual"    # 仅手动触发


class ProjectScope:
    """项目范围"""
    ALL = "all"          # 计划创建者名下的全部启用项目
    SELECTED = "selected"  # 指定的项目列表


class BatchRunStatus:
    """批次执行状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIALLY_FAILED = "partially_failed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BatchRunItemStatus:
    """批次内单项目执行状态"""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class AuditSchedule(Base):
    """定时批量审计计划"""
    __tablename__ = "audit_schedules"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    created_by = Column(String(36), ForeignKey("users.id"), nullable=False)

    # 项目范围
    project_scope = Column(String(20), default=ProjectScope.ALL, nullable=False)
    project_ids = Column(JSON, nullable=True)  # scope=selected 时的项目 ID 列表

    # 并发控制
    concurrency = Column(Integer, default=5, nullable=False)
    skip_if_running = Column(Boolean, default=True, nullable=False)  # 上一批未跑完则跳过本次触发

    # 调度配置
    schedule_type = Column(String(20), default=ScheduleType.DAILY, nullable=False)
    # 示例：{"time": "15:00"} / {"time": "15:00", "weekdays": [1, 2, 3, 4, 5]}
    #       {"interval_hours": 6} / {"run_at": "2026-09-17T15:00:00+08:00"}
    schedule_config = Column(JSON, nullable=True)
    timezone = Column(String(64), default="Asia/Shanghai", nullable=False)

    # 审计参数模板（创建 AgentTask 时使用）
    # target_vulnerabilities / verification_level / exclude_patterns /
    # max_iterations / timeout_seconds / branch_name(null = 用项目默认分支)
    task_template = Column(JSON, nullable=True)

    # 状态与执行记录
    enabled = Column(Boolean, default=True, nullable=False)
    next_run_at = Column(DateTime(timezone=True), nullable=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_run_id = Column(String(36), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # 关联关系：删除计划时保留历史批次（schedule_id 置空），故不使用级联删除
    runs = relationship(
        "AuditBatchRun",
        back_populates="schedule",
        order_by="desc(AuditBatchRun.created_at)",
    )

    __table_args__ = (
        # 调度器每轮轮询：WHERE enabled = true AND next_run_at <= now()
        Index("ix_audit_schedules_enabled_next_run", "enabled", "next_run_at"),
    )

    def __repr__(self):
        return f"<AuditSchedule {self.id} - {self.name} ({self.schedule_type})>"


class AuditBatchRun(Base):
    """一次批量审计执行记录"""
    __tablename__ = "audit_batch_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # 手动触发时可为空
    schedule_id = Column(
        String(36),
        ForeignKey("audit_schedules.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by = Column(String(36), ForeignKey("users.id"), nullable=False)
    trigger_type = Column(String(20), default="scheduled", nullable=False)  # scheduled / manual

    status = Column(String(30), default=BatchRunStatus.PENDING, nullable=False)
    concurrency = Column(Integer, default=5, nullable=False)  # 触发时的并发度快照

    # 进度计数（前端轮询实时展示）
    total_projects = Column(Integer, default=0, nullable=False)
    queued_count = Column(Integer, default=0, nullable=False)
    running_count = Column(Integer, default=0, nullable=False)
    completed_count = Column(Integer, default=0, nullable=False)
    failed_count = Column(Integer, default=0, nullable=False)
    skipped_count = Column(Integer, default=0, nullable=False)

    error_message = Column(Text, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    schedule = relationship("AuditSchedule", back_populates="runs")
    items = relationship(
        "AuditBatchRunItem",
        back_populates="run",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_audit_batch_runs_schedule_id", "schedule_id"),
        Index("ix_audit_batch_runs_status", "status"),
    )

    def __repr__(self):
        return f"<AuditBatchRun {self.id} - {self.status}>"


class AuditBatchRunItem(Base):
    """批次内单个项目的执行明细"""
    __tablename__ = "audit_batch_run_items"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = Column(
        String(36),
        ForeignKey("audit_batch_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 项目被删除后仍保留明细，故可空 + SET NULL
    project_id = Column(
        String(36),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    )
    project_name = Column(String(255), nullable=True)  # 项目名快照

    agent_task_id = Column(String(36), nullable=True)  # 关联的 AgentTask
    status = Column(String(20), default=BatchRunItemStatus.QUEUED, nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    findings_count = Column(Integer, default=0, nullable=False)
    error_message = Column(Text, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    run = relationship("AuditBatchRun", back_populates="items")

    __table_args__ = (
        Index("ix_batch_run_items_run_id", "run_id"),
        Index("ix_batch_run_items_run_status", "run_id", "status"),
        Index("ix_batch_run_items_agent_task", "agent_task_id"),
    )

    def __repr__(self):
        return f"<AuditBatchRunItem {self.id} - {self.project_name} ({self.status})>"
