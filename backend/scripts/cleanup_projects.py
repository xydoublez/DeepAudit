#!/usr/bin/env python3
"""
批量清理 DeepAudit 项目（用于批量导入测试前重置项目库）

模式:
  默认        软删除（进入回收站，可在前端 RecycleBin 恢复）
  --permanent 永久删除（不可恢复；执行前自动导出完整清单备份 CSV）

认证（二选一）:
  export DEEPAUDIT_TOKEN=<JWT>
  export DEEPAUDIT_EMAIL=xxx  export DEEPAUDIT_PASSWORD=xxx

用法:
  # 预览将清理的项目（不执行）
  .venv/bin/python scripts/cleanup_projects.py --dry-run

  # 软删除全部项目（进入回收站）
  .venv/bin/python scripts/cleanup_projects.py

  # 永久删除全部项目（清空前自动导出备份清单）
  .venv/bin/python scripts/cleanup_projects.py --permanent

  # 仅清理名称/仓库地址含关键词的项目，排除指定关键词
  .venv/bin/python scripts/cleanup_projects.py --match demo --exclude 保留,示例
"""

import argparse
import csv
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional, Tuple

import requests

DEFAULT_DEEPAUDIT_URL = os.environ.get("DEEPAUDIT_API_URL", "http://localhost:8000/api/v1")
PAGE_SIZE = 100  # 列表接口服务端分页大小

REPORT_FIELDS = [
    "id",
    "name",
    "source_type",
    "repository_url",
    "repository_type",
    "default_branch",
    "programming_languages",
    "created_at",
]


def mount_pool(session: requests.Session, size: int = 32) -> None:
    """扩大连接池并启用连接复用，支持多线程并发请求"""
    adapter = requests.adapters.HTTPAdapter(pool_connections=size, pool_maxsize=size)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


class DeepAuditClient:
    def __init__(self, base_url: str, use_proxy: bool, timeout: int):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = use_proxy
        mount_pool(self.session)
        self.token: Optional[str] = None

    def login(self, email: str, password: str) -> None:
        resp = self.session.post(
            f"{self.base_url}/auth/login",
            data={"username": email, "password": password},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"DeepAudit 登录失败 ({resp.status_code}): {resp.text[:200]}")
        self.token = resp.json()["access_token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def check_auth(self) -> None:
        resp = self.session.get(f"{self.base_url}/projects/",
                                params={"limit": 1},
                                headers=self._headers(), timeout=self.timeout)
        if resp.status_code == 401:
            raise RuntimeError("DeepAudit 认证失败 (401)，JWT 无效或已过期")
        resp.raise_for_status()

    def list_projects(self, include_deleted: bool) -> list:
        """分页拉取当前用户全部项目"""
        projects, skip = [], 0
        while True:
            resp = self.session.get(
                f"{self.base_url}/projects/",
                params={"skip": skip, "limit": PAGE_SIZE,
                        "include_deleted": "true" if include_deleted else "false"},
                headers=self._headers(), timeout=self.timeout,
            )
            resp.raise_for_status()
            batch = resp.json()
            projects.extend(batch)
            if len(batch) < PAGE_SIZE:
                return projects
            skip += PAGE_SIZE

    def delete_project(self, project_id: str, permanent: bool) -> None:
        url = f"{self.base_url}/projects/{project_id}"
        if permanent:
            url += "/permanent"
        resp = self.session.delete(url, headers=self._headers(), timeout=self.timeout)
        if resp.status_code != 200:
            raise requests.HTTPError(f"HTTP {resp.status_code}: {resp.text[:200]}", response=resp)


def project_text(project: dict) -> str:
    """用于关键词过滤的文本（名称 + 仓库地址 + 描述）"""
    keys = ("name", "repository_url", "description")
    return " ".join(str(project.get(k) or "") for k in keys).lower()


def filter_projects(projects: list, match: str, excludes: list) -> list:
    out = []
    for proj in projects:
        text = project_text(proj)
        if match and match.lower() not in text:
            continue
        if any(k and k in text for k in excludes):
            continue
        out.append(proj)
    return out


def cleanup_worker(client: DeepAuditClient, project: dict, permanent: bool,
                   retries: int = 2) -> Tuple[str, dict, str]:
    """返回 (result, project, error)；result 为 deleted / failed"""
    pid = str(project.get("id"))
    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            client.delete_project(pid, permanent)
            return ("deleted", project, "")
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", 0) if e.response is not None else 0
            last_err = str(e)
            if 400 <= status < 500:  # 4xx 不重试
                break
            if attempt < retries:
                time.sleep(1.5 * attempt)
        except requests.RequestException as e:
            last_err = str(e)
            if attempt < retries:
                time.sleep(1.5 * attempt)
    return ("failed", project, last_err)


def write_csv(path: str, rows: list, fields: list) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def main() -> int:
    parser = argparse.ArgumentParser(description="批量清理 DeepAudit 项目")
    parser.add_argument("--deepaudit-url", default=DEFAULT_DEEPAUDIT_URL,
                        help=f"DeepAudit 服务地址（默认 {DEFAULT_DEEPAUDIT_URL}）")
    parser.add_argument("--match", default="", help="仅处理名称/仓库地址/描述包含该关键词的项目")
    parser.add_argument("--exclude", default="", help="排除包含关键词的项目（逗号分隔）")
    parser.add_argument("--include-deleted", action="store_true",
                        help="处理范围包含回收站中已软删除的项目（默认只处理未删除的）")
    parser.add_argument("--permanent", action="store_true",
                        help="永久删除（默认软删除进入回收站，可恢复）")
    parser.add_argument("--concurrency", type=int, default=8, help="并发数（默认 8）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 个（0 表示不限制）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览将清理的项目，不执行删除")
    parser.add_argument("--backup-file", default="",
                        help="备份清单输出路径（默认 scripts/cleanup_backup_<时间戳>.csv）")
    parser.add_argument("--use-proxy", action="store_true",
                        help="信任系统代理环境变量（默认绕过代理直连内网）")
    parser.add_argument("--timeout", type=int, default=30, help="请求超时秒数（默认 30）")
    args = parser.parse_args()

    token_env = os.environ.get("DEEPAUDIT_TOKEN", "")
    email = os.environ.get("DEEPAUDIT_EMAIL", "")
    password = os.environ.get("DEEPAUDIT_PASSWORD", "")

    client = DeepAuditClient(args.deepaudit_url, args.use_proxy, args.timeout)
    if token_env:
        client.token = token_env
    elif email and password:
        client.login(email, password)
    else:
        print("[!] 缺少认证：请设置 DEEPAUDIT_TOKEN 或 DEEPAUDIT_EMAIL + DEEPAUDIT_PASSWORD",
              file=sys.stderr)
        return 2

    try:
        client.check_auth()
    except Exception as e:
        print(f"[!] 认证检查失败: {e}", file=sys.stderr)
        return 2

    print(f"[*] 拉取项目列表（含回收站: {'是' if args.include_deleted else '否'}）...")
    projects = client.list_projects(args.include_deleted)
    excludes = [k.strip().lower() for k in args.exclude.split(",") if k.strip()]
    targets = filter_projects(projects, args.match, excludes)
    if args.limit > 0:
        targets = targets[:args.limit]

    mode = "永久删除" if args.permanent else "软删除（进入回收站）"
    print(f"[*] 项目总数: {len(projects)}，匹配待{'永久删除' if args.permanent else '清理'}: {len(targets)}，模式: {mode}")
    if not targets:
        print("[✓] 没有需要处理的项目")
        return 0

    if args.dry_run:
        print("[*] dry-run 预览（前 10 个）:")
        for p in targets[:10]:
            print(f"    - {p.get('id')}  {p.get('name')}  {p.get('repository_url') or ''}")
        print(f"[✓] dry-run 完成，共 {len(targets)} 个将被{args.permanent and '永久删除' or '清理'}")
        return 0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    here = os.path.dirname(os.path.abspath(__file__))
    backup_path = args.backup_file or os.path.join(here, f"cleanup_backup_{ts}.csv")
    if args.permanent:
        write_csv(backup_path, targets, REPORT_FIELDS)
        print(f"[*] 已导出删除前备份清单: {backup_path}")
    else:
        backup_path = ""  # 软删可从回收站恢复，无需备份

    concurrency = max(1, args.concurrency)
    stats = {"deleted": 0, "failed": 0}
    failures = []
    print(f"[*] 开始{'永久删除' if args.permanent else '软删除'} {len(targets)} 个项目（并发 {concurrency}）...")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = pool.map(
            lambda p: cleanup_worker(client, p, args.permanent),
            targets,
        )
        for idx, (result, project, error) in enumerate(results, start=1):
            stats[result] += 1
            if result == "failed":
                failures.append({"id": project.get("id"), "name": project.get("name"),
                                 "repository_url": project.get("repository_url"), "error": error})
                print(f"    [!] 失败: {project.get('name')}  {error}")
            if idx % 200 == 0 or idx == len(targets):
                print(f"    进度: {idx}/{len(targets)}  成功 {stats['deleted']}  失败 {stats['failed']}")

    elapsed = time.time() - t0
    print(f"[✓] 清理完成: 成功 {stats['deleted']} | 失败 {stats['failed']} | 耗时 {elapsed:.1f}s")
    if failures:
        fail_path = os.path.join(here, f"cleanup_failures_{ts}.csv")
        write_csv(fail_path, failures, ["id", "name", "repository_url", "error"])
        print(f"[!] 失败明细已写入: {fail_path}")
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
