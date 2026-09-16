#!/usr/bin/env python3
"""
从 GitLab 拉取项目清单，输出为 DeepAudit「CSV 批量导入」模板格式的真实数据文件

功能:
  1. 通过 GitLab REST API (v4) 分页拉取项目清单（默认 https://gitlab.msunhis.com/api/v4）
  2. 项目描述包含「项目名称 + 负责人」；负责人取项目 owner 成员（含组继承），
     排除 --exclude-owners 指定用户名（默认 litao,lizhiqiang,xydoublez），
     无 owner 时回退 maintainer 成员，仍无则回退群组/命名空间名
  3. 技术栈默认填充 --languages 指定语言（默认 java,javascript,typescript）
  4. 输出 CSV 字段与批量导入模板完全一致（UTF-8 BOM，Excel 可直接打开）:
     name, description, source_type, repository_url, repository_type, default_branch, programming_languages
  5. 输出文件可直接在 DeepAudit 前端「项目」页通过「导入 CSV」上传，批量创建项目

用法:
  cd backend
  export sfx_git_token=<GitLab-PAT>          # 优先使用（PRIVATE-TOKEN 认证）
  .venv/bin/python scripts/import_gitlab_projects.py --dry-run       # 预览前 10 个项目
  .venv/bin/python scripts/import_gitlab_projects.py                 # 全量导出到 frontend/public + dist
  .venv/bin/python scripts/import_gitlab_projects.py --limit 50      # 只导出前 50 个
  .venv/bin/python scripts/import_gitlab_projects.py --output /tmp/projects.csv   # 自定义输出路径

输出（默认）:
  frontend/public/project_import_data.csv   # 源码目录（vite build 时复制到 dist）
  frontend/dist/project_import_data.csv     # 当前部署可直接下载
  下载地址: http://localhost:5173/project_import_data.csv

认证:
  GitLab: sfx_git_token / --gitlab-token (PAT)  >  ~/.git-credentials 同主机 Basic 认证

注意:
  - 默认绕过系统代理直连内网/本机（宿主机 http_proxy 会劫持内网流量）；如需走代理加 --use-proxy
  - 本脚本只生成 CSV 数据文件，不调用任何 DeepAudit 接口
"""

import argparse
import csv
import os
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterator, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("缺少 requests 库，请使用 backend/.venv/bin/python 运行本脚本")

DEFAULT_GITLAB_URL = "https://gitlab.msunhis.com/api/v4"
DEFAULT_LANGUAGES = ["java", "javascript", "typescript"]
OUTPUT_FILENAME = "project_import_data.csv"

# CSV 字段与 generate_project_csv_template.py 的模板保持一致
CSV_FIELDS = [
    "name",
    "description",
    "source_type",
    "repository_url",
    "repository_type",
    "default_branch",
    "programming_languages",
]


# ============================================================
# 配置与统计
# ============================================================

@dataclass
class Stats:
    fetched: int = 0      # GitLab 拉取到的项目总数
    filtered: int = 0     # 被 --group / --skip-archived 过滤
    exported: int = 0     # 生成的 CSV 行数
    fallback: int = 0     # 负责人回退群组名（无有效成员）
    parse_errors: int = 0  # 成员解析异常（已回退处理）


# ============================================================
# 通用工具
# ============================================================

def mount_pool(session: requests.Session, size: int = 32) -> None:
    """扩大连接池并启用连接复用，支持多线程并发请求"""
    adapter = requests.adapters.HTTPAdapter(pool_connections=size, pool_maxsize=size)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


def fetch_all_pages(session: requests.Session, url: str, params: dict,
                    headers: dict, timeout: int) -> Iterator[dict]:
    """按 GitLab 分页协议（X-Next-Page 头）逐页拉取所有条目"""
    page = 1
    while True:
        query = dict(params, page=page)
        resp = session.get(url, params=query, headers=headers, timeout=timeout)
        if resp.status_code == 401:
            raise RuntimeError("GitLab 认证失败 (401)，请检查 sfx_git_token 或 git-credentials")
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "5"))
            print(f"  [限流] 等待 {wait}s 后重试...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        items = resp.json()
        if not items:
            break
        yield from items
        next_page = resp.headers.get("X-Next-Page")
        if next_page:
            page = int(next_page)
        elif len(items) >= int(params.get("per_page", 100)):
            page += 1
        else:
            break


def default_outputs() -> list:
    """默认输出: frontend/public（源码目录）+ frontend/dist（部署可直接下载）"""
    here = os.path.dirname(os.path.abspath(__file__))
    fe = os.path.normpath(os.path.join(here, "..", "..", "frontend"))
    outputs = [os.path.join(fe, "public", OUTPUT_FILENAME)]
    dist_file = os.path.join(fe, "dist", OUTPUT_FILENAME)
    if os.path.isdir(os.path.dirname(dist_file)):
        outputs.append(dist_file)
    return outputs


def write_csv(path: str, rows: list) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


# ============================================================
# GitLab 客户端
# ============================================================

class GitLabClient:
    def __init__(self, base_url: str, token: Optional[str],
                 cred_file: str, use_proxy: bool, timeout: int):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = use_proxy  # False=绕过系统代理直连
        mount_pool(self.session)
        self.headers = {}
        self.auth = None

        if token:
            self.headers["PRIVATE-TOKEN"] = token
            self.auth_mode = "PRIVATE-TOKEN"
        else:
            # 回退: 使用 git credential store 中的同主机凭据做 Basic 认证
            host = urllib.parse.urlparse(self.base_url).hostname or ""
            self.auth = self._load_git_credentials(host, cred_file)
            self.auth_mode = f"Basic ({self.auth[0]})" if self.auth else "无"

    @staticmethod
    def _load_git_credentials(host: str, cred_file: str) -> Optional[Tuple[str, str]]:
        path = os.path.expanduser(cred_file)
        if not host or not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            for line in f:
                if host in line:
                    parsed = urllib.parse.urlparse(line.strip())
                    if parsed.username and parsed.password:
                        return (parsed.username, urllib.parse.unquote(parsed.password))
        return None

    def check_auth(self) -> None:
        if not self.headers and not self.auth:
            raise RuntimeError(
                "未找到 GitLab 凭据：请设置环境变量 sfx_git_token，"
                f"或在 {os.path.expanduser('~/.git-credentials')} 中配置 {self.base_url} 对应主机凭据"
            )
        resp = self.session.get(f"{self.base_url}/projects",
                                params={"per_page": 1},
                                headers=self.headers, auth=self.auth,
                                timeout=self.timeout)
        if resp.status_code == 401:
            raise RuntimeError(f"GitLab 凭据无效 (401)，认证方式: {self.auth_mode}")
        resp.raise_for_status()

    def fetch_version(self) -> str:
        try:
            resp = self.session.get(f"{self.base_url}/version",
                                    headers=self.headers, auth=self.auth,
                                    timeout=self.timeout)
            if resp.status_code == 200:
                return resp.json().get("version", "n/a")
        except requests.RequestException:
            pass
        return "n/a"

    def iter_projects(self, per_page: int = 100) -> Iterator[dict]:
        """分页拉取当前凭据可见的所有项目（稳定按 id 升序）"""
        yield from fetch_all_pages(
            self.session,
            f"{self.base_url}/projects",
            {"per_page": per_page, "order_by": "id", "sort": "asc"},
            self.headers, self.timeout,
        )

    def fetch_project_members(self, project_id: Optional[int]) -> Optional[list]:
        """获取项目全部成员（含组继承）。失败返回 None，由调用方回退群组名"""
        if project_id is None:
            return None
        url = f"{self.base_url}/projects/{project_id}/members/all"
        members: list = []
        page = 1
        while True:
            resp = None
            for attempt in range(3):
                try:
                    resp = self.session.get(url, params={"per_page": 100, "page": page},
                                            headers=self.headers, auth=self.auth,
                                            timeout=self.timeout)
                    if resp.status_code == 429:
                        time.sleep(int(resp.headers.get("Retry-After", "5")))
                        continue
                    if resp.status_code == 404:
                        return members
                    resp.raise_for_status()
                    break
                except requests.RequestException:
                    if attempt == 2:
                        return None
                    time.sleep(1.0 * (attempt + 1))
            if resp is None or resp.status_code != 200:
                return None
            batch = resp.json()
            members.extend(batch)
            if len(batch) < 100:
                return members
            page += 1


# ============================================================
# 负责人与 CSV 行构建
# ============================================================

def build_namespace_display(project: dict) -> str:
    """回退展示: 无有效成员时，个人项目取 owner，群组项目取 namespace"""
    owner = project.get("owner") or {}
    namespace = project.get("namespace") or {}
    if owner:
        name = owner.get("name") or owner.get("username") or "未知"
        username = owner.get("username")
        return f"{name} (@{username})" if username else name
    if namespace:
        if namespace.get("kind") == "group":
            return f"{namespace.get('name')} (组: {namespace.get('full_path')})"
        name = namespace.get("name") or namespace.get("path") or "未知"
        return name
    return "未知"


def pick_owner_from_members(members: Optional[list], exclude: set) -> Optional[str]:
    """从成员中挑选负责人（优先 owner，再 maintainer，排除指定用户名）。无有效成员返回 None"""
    if members is not None:
        for level in (50, 40):  # 50=Owner, 40=Maintainer
            picked = [m for m in members
                      if m.get("access_level") == level
                      and (m.get("username") or "").lower() not in exclude]
            if picked:
                return "、".join(
                    f"{m.get('name') or m.get('username')} (@{m.get('username')})" for m in picked
                )
    return None


def build_csv_row(project: dict, owner_display: str, languages: list) -> dict:
    """将 GitLab 项目对象转换为模板格式的 CSV 行"""
    name = project.get("name") or project.get("path") or str(project.get("id"))
    web_url = project.get("web_url") or ""
    repo_url = project.get("http_url_to_repo") or (web_url.rstrip("/") + ".git" if web_url else "")
    return {
        "name": name,
        "description": f"项目名称: {name}；负责人: {owner_display}；仓库: {web_url}",
        "source_type": "repository",
        "repository_url": repo_url,
        "repository_type": "gitlab",
        "default_branch": project.get("default_branch") or "main",
        "programming_languages": ",".join(languages),
    }


def build_row_worker(gitlab: GitLabClient, proj: dict, languages: list,
                     exclude_owners: set) -> Tuple[dict, str, bool, str]:
    """处理单个项目: 获取成员 -> 计算负责人 -> 生成 CSV 行
    返回 (row, gl_path, fallback, error)
    """
    gl_path = proj.get("path_with_namespace", "")
    members = None
    error = ""
    try:
        members = gitlab.fetch_project_members(proj.get("id"))
    except Exception as exc:  # 兜底：单项目异常不应中断整体导出
        error = str(exc)
    picked = pick_owner_from_members(members, exclude_owners)
    fallback = picked is None  # 无有效 owner/maintainer 成员时回退群组名
    owner_display = picked if picked else build_namespace_display(proj)
    return build_csv_row(proj, owner_display, languages), gl_path, fallback, error


# ============================================================
# 主流程
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 GitLab 拉取项目清单并导出为 DeepAudit CSV 导入模板格式（不调用 DeepAudit 接口）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true", help="仅预览将要导出的项目，不写文件")
    parser.add_argument("--limit", type=int, default=0, help="最多导出 N 个项目（0=不限制）")
    parser.add_argument("--exclude-owners", default="litao,lizhiqiang,xydoublez",
                        help="计算负责人时排除的 GitLab 用户名（逗号分隔）")
    parser.add_argument("--languages", default=",".join(DEFAULT_LANGUAGES),
                        help="填入 CSV 的技术栈（逗号分隔，小写）")
    parser.add_argument("--group", default="", help="仅导出指定群组前缀下的项目（path_with_namespace 前缀匹配，逗号分隔多个）")
    parser.add_argument("--skip-archived", action="store_true", help="跳过 GitLab 中已归档的项目")
    parser.add_argument("--gitlab-url", default=os.environ.get("GITLAB_API_URL", DEFAULT_GITLAB_URL),
                        help="GitLab API v4 地址")
    parser.add_argument("--gitlab-token", default=os.environ.get("sfx_git_token", ""),
                        help="GitLab PAT（默认取环境变量 sfx_git_token）")
    parser.add_argument("--gitlab-cred-file", default="~/.git-credentials",
                        help="无 PAT 时的凭据文件回退（Basic 认证）")
    parser.add_argument("--output", default="",
                        help="CSV 输出路径（默认写入 frontend/public 与 frontend/dist）")
    parser.add_argument("--sleep", type=float, default=0.05, help="串行模式（--concurrency 1）下每项间隔秒数")
    parser.add_argument("--concurrency", type=int, default=8, help="并发数（同时获取项目成员的并发，1=串行）")
    parser.add_argument("--per-page", type=int, default=100, help="GitLab 分页大小（最大 100）")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP 超时秒数")
    parser.add_argument("--sample", type=int, default=10, help="dry-run 预览条数")
    parser.add_argument("--use-proxy", action="store_true", help="信任系统代理环境变量（默认绕过代理直连内网）")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    stats = Stats()
    exclude_owners = {u.strip().lower() for u in args.exclude_owners.split(",") if u.strip()}
    languages = [x.strip().lower() for x in args.languages.split(",") if x.strip()]
    concurrency = max(1, args.concurrency)

    print("=" * 64)
    print("GitLab -> CSV 模板数据导出（不调用 DeepAudit 接口）")
    print("=" * 64)

    # ---------- 1. 初始化 GitLab 客户端 ----------
    gitlab = GitLabClient(args.gitlab_url, args.gitlab_token or None,
                          args.gitlab_cred_file, args.use_proxy, args.timeout)
    try:
        gitlab.check_auth()
        gl_version = gitlab.fetch_version()
        print(f"[GitLab] {args.gitlab_url} | 版本 {gl_version} | 认证 {gitlab.auth_mode}")
    except (RuntimeError, requests.RequestException) as exc:
        print(f"❌ 初始化失败: {exc}")
        return 2

    # ---------- 2. 拉取 GitLab 项目清单 ----------
    group_prefixes = [g.strip() for g in args.group.split(",") if g.strip()]
    targets = []
    print(f"[拉取] 分页获取 GitLab 项目清单 (per_page={args.per_page}) ...")
    try:
        for proj in gitlab.iter_projects(args.per_page):
            stats.fetched += 1
            path_ns = proj.get("path_with_namespace", "")
            if group_prefixes and not any(path_ns.startswith(g) for g in group_prefixes):
                stats.filtered += 1
                continue
            if args.skip_archived and proj.get("archived"):
                stats.filtered += 1
                continue
            targets.append(proj)
            if stats.fetched % 200 == 0:
                print(f"  ...已扫描 {stats.fetched} 个，待导出 {len(targets)}")
            if args.limit and len(targets) >= args.limit:
                break
    except (RuntimeError, requests.RequestException) as exc:
        print(f"❌ 拉取 GitLab 项目失败: {exc}")
        return 2

    print(f"[统计] 扫描 {stats.fetched} | 过滤 {stats.filtered} | 待导出 {len(targets)}")
    if not targets:
        print("ℹ️ 没有可导出的项目。")
        return 0

    # ---------- 3. 解析负责人并生成 CSV 行（并发） ----------
    print(f"[解析] 获取项目成员并计算负责人（并发 {concurrency}）...")
    rows: list = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = pool.map(
            lambda proj: build_row_worker(gitlab, proj, languages, exclude_owners),
            targets,
        )
        for idx, (row, gl_path, fallback, error) in enumerate(results, start=1):
            rows.append(row)
            if fallback:
                stats.fallback += 1
            if error:
                stats.parse_errors += 1
                print(f"  ⚠ [{idx}] {gl_path} 成员解析异常已回退: {error}")
            if idx % 200 == 0 or idx == len(targets):
                print(f"  [{idx}/{len(targets)}] 负责人回退群组名 {stats.fallback}")
            if concurrency == 1:
                time.sleep(args.sleep)
    stats.exported = len(rows)

    # ---------- 4. 预览（dry-run） ----------
    if args.dry_run:
        print(f"\n--- DRY-RUN 预览（前 {min(args.sample, len(rows))} 个）---")
        for row in rows[:args.sample]:
            print(f"  · {row['name']}")
            print(f"      地址: {row['repository_url']}")
            print(f"      描述: {row['description']}")
            print(f"      语言: {row['programming_languages']} | 分支: {row['default_branch']}")
        if len(rows) > args.sample:
            print(f"  ... 其余 {len(rows) - args.sample} 个略")
        print("\n✅ DRY-RUN 完成，未写入任何文件。去掉 --dry-run 即可正式导出。")
        return 0

    # ---------- 5. 写入 CSV（模板格式） ----------
    output_paths = [os.path.abspath(os.path.expanduser(args.output))] if args.output else default_outputs()
    for path in output_paths:
        write_csv(path, rows)
        print(f"[输出] {path}（{stats.exported} 行）")

    print("\n" + "=" * 64)
    print(f"导出完成: 扫描 {stats.fetched} | 过滤 {stats.filtered} | 导出 {stats.exported} 行")
    print(f"负责人回退群组名: {stats.fallback} | 成员解析异常: {stats.parse_errors}")
    print("提示: 在 DeepAudit 前端「项目」页点击「导入 CSV」上传该文件即可批量创建项目。")
    print("=" * 64)
    return 1 if stats.parse_errors else 0


if __name__ == "__main__":
    sys.exit(main())
