#!/usr/bin/env python3
"""
生成 DeepAudit 项目批量导入 CSV 模板（供「CSV 模板批量导入创建项目」使用）

本脚本只负责输出 CSV 模板文件，不执行任何导入操作。

默认输出（两处，便于源管理与直接下载）:
  frontend/public/project_import_template.csv   # 源码目录（vite build 时复制到 dist）
  frontend/dist/project_import_template.csv     # 当前部署直接下载（dist 目录存在时）

下载地址: http://localhost:5173/project_import_template.csv

CSV 字段（与 DeepAudit POST /projects/ 接口对齐）:
  name                   项目名称（必填）
  description            项目描述（可选）
  source_type            repository / zip（默认 repository）
  repository_url         仓库地址（source_type=repository 时必填）
  repository_type        github / gitlab / gitea / other（默认 other）
  default_branch         默认分支（默认 main）
  programming_languages  技术栈，逗号分隔（如 java,javascript,typescript）

用法:
  .venv/bin/python scripts/generate_project_csv_template.py
  .venv/bin/python scripts/generate_project_csv_template.py --output /path/to/template.csv
"""

import argparse
import csv
import os
import sys

FIELDS = [
    "name",
    "description",
    "source_type",
    "repository_url",
    "repository_type",
    "default_branch",
    "programming_languages",
]

SAMPLES = [
    {
        "name": "示例项目-后端服务",
        "description": "项目名称: 示例项目-后端服务；负责人: 张三 (@zhangsan)；仓库: https://gitlab.example.com/group/demo-backend",
        "source_type": "repository",
        "repository_url": "https://gitlab.example.com/group/demo-backend.git",
        "repository_type": "gitlab",
        "default_branch": "main",
        "programming_languages": "java,javascript,typescript",
    },
    {
        "name": "示例项目-前端应用",
        "description": "项目名称: 示例项目-前端应用；负责人: 李四 (@lisi)",
        "source_type": "repository",
        "repository_url": "https://github.com/example/demo-frontend.git",
        "repository_type": "github",
        "default_branch": "master",
        "programming_languages": "javascript,typescript",
    },
]


def default_outputs() -> list:
    """默认输出: frontend/public（源码目录）+ frontend/dist（部署可直接下载，若存在）"""
    here = os.path.dirname(os.path.abspath(__file__))
    fe = os.path.normpath(os.path.join(here, "..", "..", "frontend"))
    outputs = [os.path.join(fe, "public", "project_import_template.csv")]
    dist_file = os.path.join(fe, "dist", "project_import_template.csv")
    if os.path.isdir(os.path.dirname(dist_file)):
        outputs.append(dist_file)
    return outputs


def write_template(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(SAMPLES)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 DeepAudit 项目批量导入 CSV 模板")
    parser.add_argument("--output", default="",
                        help="自定义输出路径（默认写入 frontend/public 与 frontend/dist）")
    args = parser.parse_args()

    targets = [os.path.abspath(os.path.expanduser(args.output))] if args.output else default_outputs()
    for path in targets:
        write_template(path)
        print(f"模板已写入: {path}")
    print(f"字段: {', '.join(FIELDS)}")
    print("下载: http://localhost:5173/project_import_template.csv （写入 dist 后立即可用）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
