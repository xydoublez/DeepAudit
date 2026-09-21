"""
批量报告下载服务

- gitlab_client:   GitLab API v4 客户端（PAT 认证 / 分页 / 重试）
- markdown_report: Markdown 审计报告生成（与单任务导出共用）
- downloader:      批量下载执行器（并发 / 进度 / 重试 / 断点续传 / 日志 / 汇总）
"""

from app.services.report_download.downloader import (
    ReportDownloadManager,
    report_download_manager,
    build_item_output_path,
    build_report_filename,
    build_summary_markdown,
    sanitize_filename,
)
from app.services.report_download.gitlab_client import (
    GitLabApiError,
    GitLabClient,
    iter_project_ids_from_text,
    normalize_api_url,
)
from app.services.report_download.markdown_report import build_markdown_report

__all__ = [
    "ReportDownloadManager",
    "report_download_manager",
    "build_item_output_path",
    "build_report_filename",
    "build_summary_markdown",
    "sanitize_filename",
    "GitLabApiError",
    "GitLabClient",
    "iter_project_ids_from_text",
    "normalize_api_url",
    "build_markdown_report",
]
