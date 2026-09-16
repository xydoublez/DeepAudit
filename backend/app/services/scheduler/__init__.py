"""
调度器模块

提供定时批量审计能力：
- next_run:      纯函数的下次执行时间计算（零第三方依赖）
- batch_runner:  批量并发执行引擎
- scheduler:     进程内常驻调度器
"""

from .next_run import compute_next_run, describe_schedule

__all__ = ["compute_next_run", "describe_schedule"]
