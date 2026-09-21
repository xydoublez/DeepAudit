"""
批量报告下载执行器

职责：
- 按配置并发下载各项目的 Markdown 审查报告到本地目录（原子写入）
- 实时更新任务进度（总数 / 已完成 / 失败 / 跳过 / 当前下载项目）
- 失败记录原因并支持重试；支持断点续传（已完成且产物在盘的项目不重复下载）
- 任务级日志（进程日志 + 任务日志文件），完成后生成汇总报告
"""

import asyncio
import logging
import os
import re
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple

from sqlalchemy import case, func, select, update

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.agent_task import AgentFinding, AgentTask, AgentTaskStatus
from app.models.project import Project
from app.models.report_download import (
    DownloadItemStatus,
    DownloadTaskStatus,
    FilenamePattern,
    ReportDownloadItem,
    ReportDownloadTask,
)
from app.services.report_download.markdown_report import build_markdown_report

logger = logging.getLogger("app.report_download")

# 内存中的任务运行状态（进程重启后由数据库状态接管）
_RUNNING_TASKS: Dict[str, asyncio.Task] = {}
_CANCEL_EVENTS: Dict[str, asyncio.Event] = {}
# 当前正在下载的项目名（task_id -> set），运行期间由内存维护并定期写库
_CURRENT_ITEMS: Dict[str, Set[str]] = {}
# 最近日志行（task_id -> deque），供前端实时查看
_RECENT_LOGS: Dict[str, Deque[dict]] = {}

_LOG_LINE_FORMAT = "{asctime} [{levelname}] {message}"

# 漏洞排序（与单任务报告导出一致）
_SEVERITY_ORDER = case(
    (AgentFinding.severity == "critical", 1),
    (AgentFinding.severity == "high", 2),
    (AgentFinding.severity == "medium", 3),
    (AgentFinding.severity == "low", 4),
    else_=5,
)


def sanitize_filename(name: str, max_length: int = 100) -> str:
    """规范化文件/目录名：去除非法字符，压缩空白，限制长度"""
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(name or "unnamed"))
    safe = re.sub(r"\s+", "_", safe)
    safe = re.sub(r"_{2,}", "_", safe).strip("._")
    if not safe:
        safe = "unnamed"
    return safe[:max_length]


def build_report_filename(
    pattern: str,
    project: Project,
    gitlab_project_id: Optional[int] = None,
) -> str:
    """按命名规则生成报告文件名（不含目录）"""
    safe_name = sanitize_filename(project.name)
    project_ref = (
        str(gitlab_project_id) if gitlab_project_id is not None else project.id
    )
    if pattern == FilenamePattern.NAME:
        return f"{safe_name}.md"
    if pattern == FilenamePattern.ID:
        return f"{project_ref}.md"
    return f"{project_ref}_{safe_name}.md"


def build_item_output_path(
    output_dir: str,
    pattern: str,
    project: Project,
    gitlab_project_id: Optional[int] = None,
    gitlab_namespace: Optional[str] = None,
) -> Path:
    """
    构造报告产物路径：按 GitLab 命名空间（组）分类存储，
    无命名空间信息时平铺在根目录。
    """
    base = Path(output_dir)
    if gitlab_namespace:
        base = base / sanitize_filename(gitlab_namespace, max_length=80)
    return base / build_report_filename(pattern, project, gitlab_project_id)


def build_summary_markdown(task: ReportDownloadTask, items: List[ReportDownloadItem]) -> str:
    """下载完成后生成汇总报告（成功数 / 失败列表及原因 / 跳过列表）"""
    completed = [i for i in items if i.status == DownloadItemStatus.COMPLETED]
    failed = [i for i in items if i.status == DownloadItemStatus.FAILED]
    skipped = [i for i in items if i.status == DownloadItemStatus.SKIPPED]

    def _dt(value) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S") if value else "—"

    def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
        # SQLite 可能返回 naive datetime，统一补 UTC 避免减法报错
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    start_at = _as_utc(task.started_at)
    end_at = _as_utc(task.finished_at) or datetime.now(timezone.utc)
    duration = "—"
    if start_at:
        seconds = int((end_at - start_at).total_seconds())
        duration = f"{seconds // 60} 分 {seconds % 60} 秒"

    lines: List[str] = []
    lines.append("# 批量报告下载汇总")
    lines.append("")
    lines.append("| 属性 | 内容 |")
    lines.append("|------|------|")
    lines.append(f"| **任务名称** | {task.name} |")
    lines.append(f"| **任务 ID** | `{task.id}` |")
    lines.append(f"| **状态** | {task.status} |")
    lines.append(f"| **存储目录** | `{task.output_dir}` |")
    lines.append(f"| **并发数** | {task.concurrency} |")
    lines.append(f"| **开始时间** | {_dt(task.started_at)} |")
    lines.append(f"| **结束时间** | {_dt(task.finished_at)} |")
    lines.append(f"| **总耗时** | {duration} |")
    lines.append("")
    lines.append("## 下载统计")
    lines.append("")
    lines.append(f"- **项目总数:** {task.total_count}")
    lines.append(f"- **成功下载:** {len(completed)}")
    lines.append(f"- **下载失败:** {len(failed)}")
    lines.append(f"- **跳过（无可用报告）:** {len(skipped)}")
    lines.append("")

    if completed:
        lines.append("## 成功清单")
        lines.append("")
        lines.append("| 项目 | 报告文件 | 大小 |")
        lines.append("|------|----------|------|")
        for item in completed:
            size_kb = (item.file_size or 0) / 1024
            lines.append(
                f"| {item.project_name or item.project_id} | `{item.file_path}` | {size_kb:.1f} KB |"
            )
        lines.append("")

    if failed:
        lines.append("## 失败清单及原因")
        lines.append("")
        lines.append("| 项目 | 尝试次数 | 失败原因 |")
        lines.append("|------|----------|----------|")
        for item in failed:
            reason = (item.error_message or "未知错误").replace("\n", " ")[:300]
            lines.append(f"| {item.project_name or item.project_id} | {item.attempts} | {reason} |")
        lines.append("")

    if skipped:
        lines.append("## 跳过清单及原因")
        lines.append("")
        lines.append("| 项目 | 原因 |")
        lines.append("|------|------|")
        for item in skipped:
            reason = (item.error_message or "—").replace("\n", " ")[:300]
            lines.append(f"| {item.project_name or item.project_id} | {reason} |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*本汇总由 DeepAudit 批量报告下载功能自动生成*")
    lines.append("")
    return "\n".join(lines)


class ReportDownloadManager:
    """批量报告下载任务管理器"""

    # ============ 运行状态查询 ============

    @staticmethod
    def is_running(task_id: str) -> bool:
        task = _RUNNING_TASKS.get(task_id)
        return bool(task and not task.done())

    @staticmethod
    def recent_logs(task_id: str, limit: int = 200) -> List[dict]:
        return list(_RECENT_LOGS.get(task_id, deque()))[-limit:]

    # ============ 日志 ============

    @staticmethod
    def _log(
        task_id: str,
        level: str,
        message: str,
        file_logger: Optional[logging.Logger] = None,
    ) -> None:
        """写任务日志：进程 logger + 任务文件 + 内存缓冲（前端实时查看）"""
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "message": message,
        }
        buffer = _RECENT_LOGS.setdefault(task_id, deque(maxlen=500))
        buffer.append(log_entry)
        logger.log(getattr(logging, level, logging.INFO), f"[{task_id[:8]}] {message}")
        if file_logger:
            file_logger.log(getattr(logging, level, logging.INFO), message)

    @staticmethod
    def _make_file_logger(task_id: str, output_dir: str) -> Tuple[logging.Logger, str]:
        """创建任务专属文件 logger（输出到存储目录下）"""
        log_dir = Path(output_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = str(log_dir / f"download_task_{task_id[:8]}.log")
        file_logger = logging.getLogger(f"app.report_download.task.{task_id}")
        file_logger.setLevel(logging.INFO)
        file_logger.propagate = False
        for old_handler in list(file_logger.handlers):  # 防止重复运行叠加 handler
            old_handler.close()
            file_logger.removeHandler(old_handler)
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter(_LOG_LINE_FORMAT, style="{", datefmt="%Y-%m-%d %H:%M:%S")
        )
        file_logger.addHandler(handler)
        return file_logger, log_path

    @staticmethod
    def _close_file_logger(task_id: str) -> None:
        file_logger = logging.getLogger(f"app.report_download.task.{task_id}")
        for handler in list(file_logger.handlers):
            handler.close()
            file_logger.removeHandler(handler)

    # ============ 计数维护 ============

    @staticmethod
    async def _recount_task(session, task_id: str) -> None:
        """按明细实际状态重算任务计数（重试/续传/取消后校正）"""
        rows = (
            await session.execute(
                select(ReportDownloadItem.status, func.count())
                .where(ReportDownloadItem.task_id == task_id)
                .group_by(ReportDownloadItem.status)
            )
        ).all()
        counts = {status: cnt for status, cnt in rows}
        await session.execute(
            update(ReportDownloadTask)
            .where(ReportDownloadTask.id == task_id)
            .values(
                total_count=sum(counts.values()),
                completed_count=counts.get(DownloadItemStatus.COMPLETED, 0),
                failed_count=counts.get(DownloadItemStatus.FAILED, 0),
                skipped_count=counts.get(DownloadItemStatus.SKIPPED, 0),
            )
        )

    async def _bump_counter(self, task_id: str, column, delta: int = 1) -> None:
        """原子递增任务计数列，避免并发丢失更新"""
        async with async_session_factory() as session:
            await session.execute(
                update(ReportDownloadTask)
                .where(ReportDownloadTask.id == task_id)
                .values({column: column + delta})
            )
            await session.commit()

    async def _sync_current_items(self, task_id: str) -> None:
        """把内存中的当前下载项目列表写入任务行（供前端展示）"""
        names = sorted(_CURRENT_ITEMS.get(task_id, set()))
        async with async_session_factory() as session:
            await session.execute(
                update(ReportDownloadTask)
                .where(ReportDownloadTask.id == task_id)
                .values(current_items=names)
            )
            await session.commit()

    # ============ 任务启动入口 ============

    async def start(self, task_id: str, mode: str = "fresh") -> dict:
        """
        启动/继续下载任务

        mode:
          - fresh:  首次运行，处理全部 queued 明细
          - retry:  重试，把 failed 明细重置为 queued 后运行
          - resume: 断点续传，处理 queued/cancelled/running（中断遗留）
                    以及产物文件丢失的 completed 明细
        """
        if self.is_running(task_id):
            return {"message": "任务正在运行中", "task_id": task_id, "started": False}

        async with async_session_factory() as session:
            task = await session.get(ReportDownloadTask, task_id)
            if not task:
                raise ValueError("下载任务不存在")

            if mode == "retry":
                await session.execute(
                    update(ReportDownloadItem)
                    .where(
                        ReportDownloadItem.task_id == task_id,
                        ReportDownloadItem.status == DownloadItemStatus.FAILED,
                    )
                    .values(
                        status=DownloadItemStatus.QUEUED,
                        error_message=None,
                        file_path=None,
                        file_size=None,
                    )
                )
            elif mode == "resume":
                # 未完成明细重新排队
                await session.execute(
                    update(ReportDownloadItem)
                    .where(
                        ReportDownloadItem.task_id == task_id,
                        ReportDownloadItem.status.in_(
                            [
                                DownloadItemStatus.QUEUED,
                                DownloadItemStatus.RUNNING,
                                DownloadItemStatus.CANCELLED,
                            ]
                        ),
                    )
                    .values(status=DownloadItemStatus.QUEUED)
                )
                # 产物丢失的已完成明细重新排队（真正的断点续传）
                done_items = (
                    await session.execute(
                        select(ReportDownloadItem).where(
                            ReportDownloadItem.task_id == task_id,
                            ReportDownloadItem.status == DownloadItemStatus.COMPLETED,
                        )
                    )
                ).scalars().all()
                lost = [
                    item.id
                    for item in done_items
                    if not item.file_path or not Path(item.file_path).exists()
                ]
                if lost:
                    await session.execute(
                        update(ReportDownloadItem)
                        .where(ReportDownloadItem.id.in_(lost))
                        .values(status=DownloadItemStatus.QUEUED)
                    )

            await self._recount_task(session, task_id)
            await session.execute(
                update(ReportDownloadTask)
                .where(ReportDownloadTask.id == task_id)
                .values(
                    status=DownloadTaskStatus.RUNNING,
                    error_message=None,
                    finished_at=None,
                )
            )
            await session.commit()

        _CANCEL_EVENTS[task_id] = asyncio.Event()
        _CURRENT_ITEMS[task_id] = set()
        _RUNNING_TASKS[task_id] = asyncio.create_task(self._run(task_id))
        return {"message": "任务已启动", "task_id": task_id, "started": True}

    async def cancel(self, task_id: str) -> dict:
        """取消任务：停止拉取新明细，未开始的明细标记为 cancelled"""
        if not self.is_running(task_id):
            return {"message": "任务不在运行中", "task_id": task_id, "cancelled": False}
        event = _CANCEL_EVENTS.get(task_id)
        if event:
            event.set()
        return {"message": "已发送取消信号", "task_id": task_id, "cancelled": True}

    # ============ 主流程 ============

    async def _run(self, task_id: str) -> None:
        started_at = time.monotonic()
        file_logger: Optional[logging.Logger] = None
        try:
            async with async_session_factory() as session:
                task = await session.get(ReportDownloadTask, task_id)
                if not task:
                    return
                output_dir = task.output_dir
                concurrency = task.concurrency
                filename_pattern = task.filename_pattern
                if not task.started_at:
                    await session.execute(
                        update(ReportDownloadTask)
                        .where(ReportDownloadTask.id == task_id)
                        .values(started_at=func.now())
                    )
                    await session.commit()

            file_logger, log_path = self._make_file_logger(task_id, output_dir)
            async with async_session_factory() as session:
                await session.execute(
                    update(ReportDownloadTask)
                    .where(ReportDownloadTask.id == task_id)
                    .values(log_path=log_path)
                )
                await session.commit()

            self._log(task_id, "INFO", f"批量报告下载任务启动: {output_dir} (并发 {concurrency})", file_logger)

            # 待处理明细队列
            queue: asyncio.Queue[str] = asyncio.Queue()
            async with async_session_factory() as session:
                item_ids = (
                    await session.execute(
                        select(ReportDownloadItem.id)
                        .where(
                            ReportDownloadItem.task_id == task_id,
                            ReportDownloadItem.status == DownloadItemStatus.QUEUED,
                        )
                        .order_by(ReportDownloadItem.created_at)
                    )
                ).scalars().all()
            for item_id in item_ids:
                queue.put_nowait(item_id)
            self._log(task_id, "INFO", f"待下载项目数: {queue.qsize()}", file_logger)

            cancel_event = _CANCEL_EVENTS[task_id]
            semaphore = asyncio.Semaphore(concurrency)

            async def worker() -> None:
                while not cancel_event.is_set():
                    try:
                        item_id = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    async with semaphore:
                        if cancel_event.is_set():
                            queue.put_nowait(item_id)
                            return
                        await self._download_item(task_id, item_id, output_dir, filename_pattern, file_logger)

            workers = [asyncio.create_task(worker()) for _ in range(min(concurrency, max(1, queue.qsize())))]
            await asyncio.gather(*workers)

            cancelled = cancel_event.is_set()
            await self._finalize(task_id, cancelled, file_logger)
            elapsed = int(time.monotonic() - started_at)
            self._log(task_id, "INFO", f"任务结束，总耗时 {elapsed // 60} 分 {elapsed % 60} 秒", file_logger)
        except Exception as exc:  # noqa: BLE001 - 后台任务兜底
            logger.exception("下载任务 %s 异常终止", task_id)
            self._log(task_id, "ERROR", f"任务异常终止: {exc}", file_logger)
            try:
                await self._finalize(task_id, False, file_logger, error=str(exc))
            except Exception:  # noqa: BLE001
                logger.exception("下载任务 %s 收尾失败", task_id)
        finally:
            self._close_file_logger(task_id)
            _CURRENT_ITEMS.pop(task_id, None)
            _RUNNING_TASKS.pop(task_id, None)
            _CANCEL_EVENTS.pop(task_id, None)

    async def _download_item(
        self,
        task_id: str,
        item_id: str,
        output_dir: str,
        filename_pattern: str,
        file_logger: Optional[logging.Logger],
    ) -> None:
        """下载单个项目的报告（独立数据库会话 + 原子写入）"""
        async with async_session_factory() as session:
            item = await session.get(ReportDownloadItem, item_id)
            if not item or item.status == DownloadItemStatus.COMPLETED:
                return

            display_name = item.project_name or item.project_id or item_id
            item.status = DownloadItemStatus.RUNNING
            item.attempts = (item.attempts or 0) + 1
            item.started_at = func.now()
            item.error_message = None
            await session.commit()

        _CURRENT_ITEMS.setdefault(task_id, set()).add(display_name)
        await self._sync_current_items(task_id)
        self._log(task_id, "INFO", f"开始下载报告: {display_name}", file_logger)

        try:
            async with async_session_factory() as session:
                project = None
                if item.project_id:
                    project = await session.get(Project, item.project_id)
                if not project:
                    raise RuntimeError("项目不存在或已被删除")

                # 找最近一次已完成的审计任务作为报告数据来源
                agent_task = (
                    await session.execute(
                        select(AgentTask)
                        .where(
                            AgentTask.project_id == project.id,
                            AgentTask.status == AgentTaskStatus.COMPLETED,
                        )
                        .order_by(AgentTask.created_at.desc())
                        .limit(1)
                    )
                ).scalars().first()
                if not agent_task:
                    async with async_session_factory() as s2:
                        db_item = await s2.get(ReportDownloadItem, item_id)
                        db_item.status = DownloadItemStatus.SKIPPED
                        db_item.error_message = "项目尚无已完成的审计任务，无报告可下载"
                        db_item.finished_at = func.now()
                        await s2.commit()
                    await self._bump_counter(task_id, ReportDownloadTask.skipped_count)
                    self._log(task_id, "WARNING", f"跳过 {display_name}: 项目尚无已完成的审计任务", file_logger)
                    return

                findings = (
                    await session.execute(
                        select(AgentFinding)
                        .where(AgentFinding.task_id == agent_task.id)
                        .order_by(_SEVERITY_ORDER, AgentFinding.created_at.desc())
                    )
                ).scalars().all()

                content = build_markdown_report(agent_task, project, findings)
                item_agent_task_id = agent_task.id
                item_gitlab_project_id = item.gitlab_project_id
                item_gitlab_namespace = item.gitlab_namespace

            # 生成产物路径（命名空间分类 + 规范化命名）
            target = build_item_output_path(
                output_dir,
                filename_pattern,
                project,
                item_gitlab_project_id,
                item_gitlab_namespace,
            )
            target.parent.mkdir(parents=True, exist_ok=True)

            # 原子写入：先写临时文件再替换，保证断点续传时已存在文件一定是完整的
            tmp_path = target.with_name(f".{target.name}.tmp-{uuid.uuid4().hex[:8]}")
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, target)

            async with async_session_factory() as session:
                db_item = await session.get(ReportDownloadItem, item_id)
                db_item.status = DownloadItemStatus.COMPLETED
                db_item.agent_task_id = item_agent_task_id
                db_item.file_path = str(target)
                db_item.file_size = target.stat().st_size
                db_item.finished_at = func.now()
                db_item.error_message = None
                await session.commit()

            await self._bump_counter(task_id, ReportDownloadTask.completed_count)
            size_kb = target.stat().st_size / 1024
            self._log(
                task_id,
                "INFO",
                f"报告下载成功: {display_name} -> {target} ({size_kb:.1f} KB, 漏洞 {len(findings)} 个)",
                file_logger,
            )
        except Exception as exc:  # noqa: BLE001 - 单项失败不影响整体
            error_msg = f"{type(exc).__name__}: {exc}"
            self._log(task_id, "ERROR", f"报告下载失败: {display_name} - {error_msg}", file_logger)
            try:
                async with async_session_factory() as session:
                    db_item = await session.get(ReportDownloadItem, item_id)
                    db_item.status = DownloadItemStatus.FAILED
                    db_item.error_message = error_msg[:2000]
                    db_item.finished_at = func.now()
                    await session.commit()
                await self._bump_counter(task_id, ReportDownloadTask.failed_count)
            except Exception:  # noqa: BLE001
                logger.exception("记录下载明细失败状态时出错 item=%s", item_id)
        finally:
            names = _CURRENT_ITEMS.get(task_id)
            if names is not None:
                names.discard(display_name)
                await self._sync_current_items(task_id)

    async def _finalize(
        self,
        task_id: str,
        cancelled: bool,
        file_logger: Optional[logging.Logger],
        error: Optional[str] = None,
    ) -> None:
        """收尾：取消未处理明细、重算计数、生成汇总报告、落最终状态"""
        async with async_session_factory() as session:
            if cancelled:
                await session.execute(
                    update(ReportDownloadItem)
                    .where(
                        ReportDownloadItem.task_id == task_id,
                        ReportDownloadItem.status.in_(
                            [DownloadItemStatus.QUEUED, DownloadItemStatus.RUNNING]
                        ),
                    )
                    .values(
                        status=DownloadItemStatus.CANCELLED,
                        error_message="任务被取消",
                    )
                )
            await self._recount_task(session, task_id)
            await session.commit()

        try:
            async with async_session_factory() as session:
                task = await session.get(ReportDownloadTask, task_id)
                if not task:
                    return
                items = (
                    await session.execute(
                        select(ReportDownloadItem)
                        .where(ReportDownloadItem.task_id == task_id)
                        .order_by(ReportDownloadItem.created_at)
                    )
                ).scalars().all()
                counts: Dict[str, int] = {}
                for i in items:
                    counts[i.status] = counts.get(i.status, 0) + 1

                # 最终状态
                if error:
                    final_status = DownloadTaskStatus.FAILED
                elif cancelled:
                    final_status = DownloadTaskStatus.CANCELLED
                elif counts.get(DownloadItemStatus.FAILED, 0) > 0:
                    final_status = DownloadTaskStatus.PARTIALLY_FAILED
                elif task.total_count > 0 and counts.get(
                    DownloadItemStatus.COMPLETED, 0
                ) + counts.get(DownloadItemStatus.SKIPPED, 0) >= task.total_count:
                    final_status = DownloadTaskStatus.COMPLETED
                else:
                    final_status = DownloadTaskStatus.PARTIALLY_FAILED

                # 生成汇总报告（尽力而为，失败不影响任务状态落库）
                summary_path = task.summary_path
                if task.output_dir:
                    try:
                        summary_content = build_summary_markdown(task, list(items))
                        summary_dir = Path(task.output_dir)
                        summary_dir.mkdir(parents=True, exist_ok=True)
                        summary_file = summary_dir / f"download_summary_{task_id[:8]}.md"
                        summary_file.write_text(summary_content, encoding="utf-8")
                        summary_path = str(summary_file)
                    except OSError as exc:
                        self._log(task_id, "ERROR", f"生成汇总报告失败: {exc}", file_logger)

                await session.execute(
                    update(ReportDownloadTask)
                    .where(ReportDownloadTask.id == task_id)
                    .values(
                        status=final_status,
                        error_message=error,
                        summary_path=summary_path,
                        finished_at=func.now(),
                        current_items=[],
                    )
                )
                await session.commit()
                self._log(
                    task_id,
                    "INFO",
                    f"任务收尾完成，状态: {final_status}，汇总报告: {summary_path or '未生成'}",
                    file_logger,
                )
        except Exception as exc:  # noqa: BLE001
            self._log(task_id, "ERROR", f"任务收尾失败: {exc}", file_logger)


# 全局单例
report_download_manager = ReportDownloadManager()
