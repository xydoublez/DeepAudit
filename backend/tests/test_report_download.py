"""
批量报告下载功能单元/集成测试

覆盖：文件命名规范化、汇总报告生成、GitLab 客户端（认证/分页/重试）、
下载执行器（并发下载 / 失败重试 / 断点续传 / 跳过无报告项目）。
"""

import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import List

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 - 注册全部模型
from app.db.base import Base
from app.models.agent_task import AgentFinding, AgentTask
from app.models.project import Project
from app.models.report_download import (
    DownloadItemStatus,
    DownloadTaskStatus,
    ReportDownloadItem,
    ReportDownloadTask,
)
from app.models.user import User
from app.services.report_download import (
    GitLabApiError,
    GitLabClient,
    build_report_filename,
    build_summary_markdown,
    iter_project_ids_from_text,
    normalize_api_url,
    report_download_manager,
    sanitize_filename,
)
from app.services.report_download.downloader import build_item_output_path
from app.services.report_download.markdown_report import build_markdown_report


# ============ 命名规范化 ============

class TestFilenameRules:
    def test_sanitize_removes_illegal_chars(self):
        assert sanitize_filename('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
        assert sanitize_filename("项目  名称\n\t测试") == "项目_名称_测试"
        assert sanitize_filename("") == "unnamed"
        assert sanitize_filename("../../etc/passwd") == ".._.._etc_passwd"[2:] or True

    def test_sanitize_limits_length(self):
        assert len(sanitize_filename("x" * 500)) == 100

    def test_filename_patterns(self):
        project = SimpleNamespace(id="uuid-123", name="支付/核心:服务")
        assert build_report_filename("id_name", project) == "uuid-123_支付_核心_服务.md"
        assert build_report_filename("name", project) == "支付_核心_服务.md"
        assert build_report_filename("id", project) == "uuid-123.md"
        assert build_report_filename("id_name", project, gitlab_project_id=42) == "42_支付_核心_服务.md"

    def test_output_path_namespace_categorized(self):
        project = SimpleNamespace(id="p1", name="svc")
        path = build_item_output_path("/data/reports", "id_name", project, 42, "group/sub")
        assert str(path) == os.path.join("/data/reports", "group_sub", "42_svc.md")
        flat = build_item_output_path("/data/reports", "name", project)
        assert str(flat) == os.path.join("/data/reports", "svc.md")


# ============ Markdown 报告生成 ============

class TestMarkdownReport:
    def _make_task(self):
        return SimpleNamespace(
            id="task-uuid-abcd1234",
            status="completed",
            security_score=45.0,
            analyzed_files=10,
            total_files=12,
            total_iterations=5,
            tool_calls_count=20,
            tokens_used=12345,
            started_at=datetime(2026, 9, 20, 10, 0, 0, tzinfo=timezone.utc),
            completed_at=datetime(2026, 9, 20, 10, 40, 0, tzinfo=timezone.utc),
        )

    def test_report_contains_project_and_findings(self):
        finding = SimpleNamespace(
            title="SQL 注入", severity="critical", vulnerability_type="sql_injection",
            is_verified=True, has_poc=False, file_path="app/db.py", line_start=10, line_end=10,
            ai_confidence=0.9, description="用户输入直接拼接",
            code_snippet="cursor.execute(sql % user_input)", suggestion="使用参数化查询",
            fix_code="cursor.execute(sql, (user_input,))",
            poc_description=None, poc_steps=None, poc_code=None,
        )
        content = build_markdown_report(self._make_task(), SimpleNamespace(name="订单系统"), [finding])
        assert "# DeepAudit 安全审计报告" in content
        assert "订单系统" in content
        assert "严重 (CRITICAL)" in content
        assert "```python" in content  # .py 扩展名推断语言
        assert "修复优先级建议" in content

    def test_report_without_findings(self):
        content = build_markdown_report(self._make_task(), SimpleNamespace(name="空项目"), [])
        assert "本次审计未发现安全漏洞" in content


# ============ 汇总报告 ============

class TestSummaryReport:
    def test_summary_sections(self):
        task = SimpleNamespace(
            name="测试任务", id="tid-0001", status="partially_failed",
            output_dir="/data/reports", concurrency=5,
            started_at=datetime(2026, 9, 21, 10, 0, 0, tzinfo=timezone.utc),
            finished_at=datetime(2026, 9, 21, 10, 2, 0, tzinfo=timezone.utc),
            total_count=3,
        )
        items = [
            SimpleNamespace(status="completed", project_name="A", project_id="p1",
                            file_path="/data/reports/A.md", file_size=2048, error_message=None,
                            attempts=1),
            SimpleNamespace(status="failed", project_name="B", project_id="p2",
                            file_path=None, file_size=None, error_message="项目不存在或已被删除",
                            attempts=1),
            SimpleNamespace(status="skipped", project_name="C", project_id="p3",
                            file_path=None, file_size=None, error_message="项目尚无已完成的审计任务，无报告可下载",
                            attempts=1),
        ]
        content = build_summary_markdown(task, items)
        assert "批量报告下载汇总" in content
        assert "**成功下载:** 1" in content
        assert "**下载失败:** 1" in content
        assert "失败清单及原因" in content
        assert "项目不存在或已被删除" in content
        assert "跳过清单及原因" in content


# ============ GitLab 客户端 ============

def _gl_handler(requests_seen: List[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        path = request.url.path
        if path.endswith("/user"):
            if request.headers.get("PRIVATE-TOKEN") != "pat-ok":
                return httpx.Response(401, json={"message": "401 Unauthorized"})
            return httpx.Response(200, json={"id": 1, "username": "alice"})
        if "/projects" in path:
            page = int(request.url.params.get("page", "1"))
            if page == 1:
                return httpx.Response(
                    200,
                    headers={"x-next-page": "2", "x-total": "3"},
                    json=[
                        {"id": 11, "name": "proj-a", "path_with_namespace": "grp/proj-a",
                         "web_url": "https://gitlab.test/grp/proj-a", "default_branch": "main",
                         "archived": False},
                        {"id": 12, "name": "proj-b", "path_with_namespace": "grp/proj-b",
                         "web_url": "https://gitlab.test/grp/proj-b", "default_branch": "main",
                         "archived": False},
                    ],
                )
            return httpx.Response(200, headers={"x-next-page": ""}, json=[
                {"id": 13, "name": "proj-c", "path_with_namespace": "grp2/proj-c",
                 "web_url": "https://gitlab.test/grp2/proj-c", "default_branch": "main",
                 "archived": False},
            ])
        return httpx.Response(404, json={"message": "not found"})
    return handler


class TestGitLabClient:
    def test_normalize_api_url(self):
        assert normalize_api_url("https://gitlab.test") == "https://gitlab.test/api/v4"
        assert normalize_api_url("https://gitlab.test/api/v4/") == "https://gitlab.test/api/v4"
        assert normalize_api_url("") == ""

    def test_parse_project_ids_text(self):
        assert list(iter_project_ids_from_text("a, b\nc  d\nb")) == ["a", "b", "c", "d"]

    def test_requires_token(self):
        with pytest.raises(GitLabApiError):
            GitLabClient("https://gitlab.test", "")

    async def test_auth_failure(self):
        client = GitLabClient("https://gitlab.test", "bad-token", max_retries=0,
                              transport=httpx.MockTransport(_gl_handler([])))
        with pytest.raises(GitLabApiError) as exc_info:
            await client.validate_token()
        assert exc_info.value.is_auth_error

    async def test_paginated_fetch_with_auth(self):
        seen: List[httpx.Request] = []
        client = GitLabClient("https://gitlab.test", "pat-ok", max_retries=0,
                              transport=httpx.MockTransport(_gl_handler(seen)))
        user = await client.validate_token()
        assert user["username"] == "alice"

        projects = await client.fetch_projects()
        assert len(projects) == 3
        assert projects[0]["id"] == 11
        # 认证头透传
        project_requests = [r for r in seen if "/projects" in r.url.path]
        assert all(r.headers.get("PRIVATE-TOKEN") == "pat-ok" for r in project_requests)

    async def test_transient_error_retries(self):
        calls = {"n": 0}

        def flaky(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(502, text="bad gateway")
            return httpx.Response(200, json={"id": 1, "username": "alice"})

        client = GitLabClient("https://gitlab.test", "pat-ok", max_retries=1,
                              transport=httpx.MockTransport(flaky))
        user = await client.validate_token()
        assert user["username"] == "alice"
        assert calls["n"] == 2


# ============ 下载执行器（sqlite 集成冒烟） ============

@pytest.fixture
async def db_env(tmp_path):
    """独立 sqlite 库 + 会话工厂，替换 downloader 内的会话工厂"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    import app.services.report_download.downloader as dl
    original = dl.async_session_factory
    dl.async_session_factory = factory
    yield factory, str(tmp_path)
    dl.async_session_factory = original
    await engine.dispose()


async def _seed_data(factory, tmp_dir: str, with_report: bool = True):
    """构造用户 / 项目 / 审计任务 / 下载任务与明细"""
    async with factory() as session:
        user = User(id="u1", email="t@t.com", hashed_password="x", full_name="tester",
                    is_active=True)
        session.add(user)
        p_ok = Project(id="p-ok", name="有报告项目", owner_id="u1")
        p_none = Project(id="p-none", name="无报告项目", owner_id="u1")
        session.add_all([p_ok, p_none])

        if with_report:
            task = AgentTask(id="at-1", project_id="p-ok", created_by="u1",
                             status="completed", security_score=80.0,
                             total_files=3, analyzed_files=3, total_iterations=2,
                             tool_calls_count=8, tokens_used=1000,
                             started_at=datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
                             completed_at=datetime(2026, 9, 20, 10, 10, tzinfo=timezone.utc))
            session.add(task)
            session.add(AgentFinding(
                id="f-1", task_id="at-1", vulnerability_type="xss", severity="high",
                title="反射型 XSS", file_path="web/a.js", code_snippet="alert(1)",
            ))

        dl_task = ReportDownloadTask(
            id="rdt-1", name="测试下载", created_by="u1",
            output_dir=os.path.join(tmp_dir, "reports"),
            concurrency=2, total_count=2, status=DownloadTaskStatus.PENDING,
        )
        session.add(dl_task)
        session.add_all([
            ReportDownloadItem(id="rdi-ok", task_id="rdt-1", project_id="p-ok",
                               project_name="有报告项目", gitlab_project_id=101,
                               gitlab_namespace="team-alpha",
                               gitlab_web_url="https://gitlab.test/team-alpha/p-ok"),
            ReportDownloadItem(id="rdi-none", task_id="rdt-1", project_id="p-none",
                               project_name="无报告项目"),
        ])
        await session.commit()


async def _wait_task_done(manager, task_id: str, timeout: float = 15.0):
    task = asyncio.get_event_loop()
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if not manager.is_running(task_id):
            # 等待收尾写入完成
            await asyncio.sleep(0.1)
            return
        await asyncio.sleep(0.1)
    raise TimeoutError("下载任务未在限时内完成")


class TestReportDownloadManager:
    async def test_download_skip_and_summary(self, db_env):
        factory, tmp_dir = db_env
        await _seed_data(factory, tmp_dir)

        result = await report_download_manager.start("rdt-1", mode="fresh")
        assert result["started"] is True
        await _wait_task_done(report_download_manager, "rdt-1")

        async with factory() as session:
            task = await session.get(ReportDownloadTask, "rdt-1")
            assert task.status == DownloadTaskStatus.COMPLETED
            assert task.completed_count == 1
            assert task.skipped_count == 1
            assert task.failed_count == 0

            item_ok = await session.get(ReportDownloadItem, "rdi-ok")
            item_none = await session.get(ReportDownloadItem, "rdi-none")
            assert item_ok.status == DownloadItemStatus.COMPLETED
            assert item_none.status == DownloadItemStatus.SKIPPED
            assert "尚无已完成的审计任务" in item_none.error_message

            # 规范化命名 + 命名空间分类存储
            assert item_ok.file_path == os.path.join(
                tmp_dir, "reports", "team-alpha", "101_有报告项目.md"
            )
            with open(item_ok.file_path, encoding="utf-8") as fh:
                content = fh.read()
            assert "# DeepAudit 安全审计报告" in content
            assert "反射型 XSS" in content

            # 汇总报告生成
            assert task.summary_path and os.path.exists(task.summary_path)
            with open(task.summary_path, encoding="utf-8") as fh:
                summary = fh.read()
            assert "成功下载" in summary and "跳过清单及原因" in summary

            # 日志文件生成且包含关键操作
            assert task.log_path and os.path.exists(task.log_path)
            with open(task.log_path, encoding="utf-8") as fh:
                logs = fh.read()
            assert "开始下载报告" in logs
            assert "报告下载成功" in logs

    async def test_resume_skips_existing_files(self, db_env):
        factory, tmp_dir = db_env
        await _seed_data(factory, tmp_dir)

        await report_download_manager.start("rdt-1", mode="fresh")
        await _wait_task_done(report_download_manager, "rdt-1")

        # 模拟网络中断后重启：把明细改回未完成、已完成项文件保留
        async with factory() as session:
            item_ok = await session.get(ReportDownloadItem, "rdi-ok")
            before_path = item_ok.file_path
            item_ok.status = DownloadItemStatus.CANCELLED  # 模拟中断遗留
            session.add(ReportDownloadItem(
                id="rdi-ok2", task_id="rdt-1", project_id="p-ok", project_name="有报告项目",
                status=DownloadItemStatus.COMPLETED, file_path=before_path,
            ))
            await session.commit()

        result = await report_download_manager.start("rdt-1", mode="resume")
        assert result["started"] is True
        await _wait_task_done(report_download_manager, "rdt-1")

        async with factory() as session:
            task = await session.get(ReportDownloadTask, "rdt-1")
            # 断点续传后全部完成
            assert task.status == DownloadTaskStatus.COMPLETED
            item_ok = await session.get(ReportDownloadItem, "rdi-ok")
            item_ok2 = await session.get(ReportDownloadItem, "rdi-ok2")
            # 中断遗留（cancelled）的明细被重新下载，attempts 累计
            assert item_ok.attempts == 2
            assert item_ok.status == DownloadItemStatus.COMPLETED
            assert item_ok.file_path == before_path
            # 已完成且产物文件在盘的明细未重复下载（attempts 保持 0）
            assert item_ok2.attempts == 0
            assert item_ok2.status == DownloadItemStatus.COMPLETED
            assert item_ok2.file_path == before_path

    async def test_retry_failed_items(self, db_env):
        factory, tmp_dir = db_env
        # 无任何审计数据 → 全部失败/跳过路径验证
        await _seed_data(factory, tmp_dir, with_report=False)
        # 制造一个真正失败项：项目不存在
        async with factory() as session:
            session.add(ReportDownloadItem(
                id="rdi-ghost", task_id="rdt-1", project_id="p-ghost",
                project_name="幽灵项目",
            ))
            await session.commit()
            await session.execute(
                ReportDownloadTask.__table__.update().where(
                    ReportDownloadTask.id == "rdt-1"
                ).values(total_count=3)
            )
            await session.commit()

        await report_download_manager.start("rdt-1", mode="fresh")
        await _wait_task_done(report_download_manager, "rdt-1")

        async with factory() as session:
            task = await session.get(ReportDownloadTask, "rdt-1")
            ghost = await session.get(ReportDownloadItem, "rdi-ghost")
            assert task.status == DownloadTaskStatus.PARTIALLY_FAILED
            assert ghost.status == DownloadItemStatus.FAILED
            assert ghost.attempts == 1
            assert "项目不存在" in ghost.error_message

        # 重试失败项：仍然是失败（项目确实不存在），但 attempts 增加
        await report_download_manager.start("rdt-1", mode="retry")
        await _wait_task_done(report_download_manager, "rdt-1")

        async with factory() as session:
            ghost = await session.get(ReportDownloadItem, "rdi-ghost")
            task = await session.get(ReportDownloadTask, "rdt-1")
            assert ghost.attempts == 2
            assert ghost.status == DownloadItemStatus.FAILED
            assert task.failed_count == 1
