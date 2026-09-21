"""
批量报告下载模型

支持「一个下载任务 → 并发下载 N 个项目的 Markdown 审查报告」的能力：

- ReportDownloadTask:  一次批量下载任务（配置 / 进度计数 / 汇总信息）
- ReportDownloadItem:  任务内单个项目的下载明细（状态 / 失败原因 / 产物路径）

断点续传：任务与明细状态持久化在数据库中，进程重启后可对
queued / cancelled / 产物丢失的明细继续下载，已完成项不重复下载。
"""

import uuid

from sqlalchemy import (
    Column, String, Integer, Text, Boolean,
    DateTime, ForeignKey, JSON, Index,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base


class DownloadTaskStatus:
    """下载任务状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIALLY_FAILED = "partially_failed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DownloadItemStatus:
    """任务内单项目下载状态"""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"      # 无可用审计报告等原因跳过
    CANCELLED = "cancelled"


class FilenamePattern:
    """报告文件命名规则"""
    ID_NAME = "id_name"      # {项目ID}_{项目名称}.md
    NAME = "name"            # {项目名称}.md
    ID = "id"                # {项目ID}.md


class ReportDownloadTask(Base):
    """批量报告下载任务"""
    __tablename__ = "report_download_tasks"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    created_by = Column(String(36), ForeignKey("users.id"), nullable=False)

    # 下载配置
    output_dir = Column(String(512), nullable=False)      # 报告存储目录（服务端本地路径）
    concurrency = Column(Integer, default=5, nullable=False)
    filename_pattern = Column(String(20), default=FilenamePattern.ID_NAME, nullable=False)
    # GitLab 来源信息（用于分类存储与汇总，不保存 Token）
    gitlab_url = Column(String(512), nullable=True)
    gitlab_ref = Column(String(255), nullable=True)       # 获取项目清单时的过滤描述

    # 进度计数（实时更新，供前端轮询）
    total_count = Column(Integer, default=0, nullable=False)
    completed_count = Column(Integer, default=0, nullable=False)
    failed_count = Column(Integer, default=0, nullable=False)
    skipped_count = Column(Integer, default=0, nullable=False)
    # 当前正在下载的项目名称列表（JSON 数组）
    current_items = Column(JSON, nullable=True)

    # 状态与结果
    status = Column(String(20), default=DownloadTaskStatus.PENDING, nullable=False)
    error_message = Column(Text, nullable=True)
    summary_path = Column(String(512), nullable=True)     # 汇总报告文件路径
    log_path = Column(String(512), nullable=True)         # 任务日志文件路径

    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    items = relationship(
        "ReportDownloadItem",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="ReportDownloadItem.created_at",
    )

    def __repr__(self):
        return f"<ReportDownloadTask {self.id} - {self.status}>"


class ReportDownloadItem(Base):
    """批量下载任务内单项目明细"""
    __tablename__ = "report_download_items"
    __table_args__ = (
        Index("ix_report_download_items_task_status", "task_id", "status"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(String(36), ForeignKey("report_download_tasks.id", ondelete="CASCADE"), nullable=False)

    # 项目信息（project_id 为 DeepAudit 内部项目 ID）
    project_id = Column(String(36), nullable=True)
    project_name = Column(String(255), nullable=True)
    # GitLab 侧信息（通过 GitLab API 批量获取时填充）
    gitlab_project_id = Column(Integer, nullable=True)
    gitlab_namespace = Column(String(512), nullable=True)   # 所属组/命名空间，用于分类存储
    gitlab_web_url = Column(String(512), nullable=True)

    # 关联的审计任务（报告数据来源）
    agent_task_id = Column(String(36), nullable=True)

    status = Column(String(20), default=DownloadItemStatus.QUEUED, nullable=False)
    attempts = Column(Integer, default=0, nullable=False)   # 已尝试次数（重试累计）
    error_message = Column(Text, nullable=True)

    # 产物信息
    file_path = Column(String(512), nullable=True)
    file_size = Column(Integer, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    task = relationship("ReportDownloadTask", back_populates="items")

    def __repr__(self):
        return f"<ReportDownloadItem {self.project_name} - {self.status}>"
