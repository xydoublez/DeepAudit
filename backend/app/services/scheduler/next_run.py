"""
下次执行时间计算（纯函数、零第三方依赖）

不引入 APScheduler / croniter：Docker 构建使用 `uv sync --frozen`，
新增依赖必须同步重生成 uv.lock，否则构建直接失败。
这里用标准库 `zoneinfo` + `datetime` 覆盖五种调度类型：

    once      {"run_at": "2026-09-17T15:00:00+08:00"}
    daily     {"time": "15:00"}
    weekly    {"time": "15:00", "weekdays": [1, 2, 3, 4, 5]}   # 1=周一 ... 7=周日
    interval  {"interval_hours": 6}
    manual    {}                                                 # 永不自动触发

约定：
- 入参 `after` 为「基准时刻」（通常是当前时间或上次触发时间）
- 计算全部在计划配置的时区内完成
- 返回值统一转为 UTC（tz-aware），便于直接入库与比较
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_INTERVAL_HOURS = 24

# 调度类型常量（与 app.models.batch_audit.ScheduleType 保持一致，
# 此处不直接 import 以避免服务层依赖模型层）
ONCE = "once"
DAILY = "daily"
WEEKLY = "weekly"
INTERVAL = "interval"
MANUAL = "manual"

VALID_SCHEDULE_TYPES = (ONCE, DAILY, WEEKLY, INTERVAL, MANUAL)


def _get_timezone(tz_name: Optional[str]) -> ZoneInfo:
    """解析时区名，非法或缺省时回退到默认时区"""
    name = (tz_name or DEFAULT_TIMEZONE).strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(f"[Scheduler] Unknown timezone '{name}', falling back to {DEFAULT_TIMEZONE}")
        return ZoneInfo(DEFAULT_TIMEZONE)


def _ensure_aware(value: datetime, tz: ZoneInfo) -> datetime:
    """naive datetime 按指定时区解释，aware datetime 原样返回"""
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value


def _parse_clock_time(raw: Any) -> timedelta:
    """解析 "HH:MM" / "HH:MM:SS" 为当天的时间偏移"""
    if not raw or not isinstance(raw, str):
        raise ValueError("schedule_config.time 必须是 'HH:MM' 格式的字符串")

    parts = raw.strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"无法解析时间 '{raw}'，应为 'HH:MM' 或 'HH:MM:SS'")

    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        raise ValueError(f"无法解析时间 '{raw}'，应为 'HH:MM' 或 'HH:MM:SS'")

    hour = numbers[0]
    minute = numbers[1] if len(numbers) > 1 else 0
    second = numbers[2] if len(numbers) > 2 else 0

    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError(f"时间 '{raw}' 超出合法范围")

    return timedelta(hours=hour, minutes=minute, seconds=second)


def _parse_weekdays(raw: Any) -> List[int]:
    """解析星期列表（1=周一 ... 7=周日），非法值直接报错"""
    if raw is None:
        return [1, 2, 3, 4, 5, 6, 7]

    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("schedule_config.weekdays 必须是非空数组")

    weekdays: List[int] = []
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            raise ValueError(f"weekdays 含非法值 '{item}'，应为 1-7 的整数")
        if not 1 <= value <= 7:
            raise ValueError(f"weekdays 含非法值 '{value}'，应为 1-7 的整数（1=周一）")
        if value not in weekdays:
            weekdays.append(value)

    return sorted(weekdays)


def _start_of_local_day(moment: datetime, tz: ZoneInfo) -> datetime:
    """取 moment 在 tz 时区当天的 00:00:00"""
    local = moment.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _next_daily(after_local: datetime, offset: timedelta, tz: ZoneInfo) -> datetime:
    """每天固定时间：当天未到点则今天，否则顺延到次日"""
    candidate = _start_of_local_day(after_local, tz) + offset
    if candidate <= after_local:
        candidate = _start_of_local_day(after_local + timedelta(days=1), tz) + offset
    return candidate


def _next_weekly(
    after_local: datetime,
    offset: timedelta,
    weekdays: List[int],
    tz: ZoneInfo,
) -> datetime:
    """每周指定星期 + 固定时间：向后最多找 8 天"""
    for delta_days in range(0, 8):
        day = _start_of_local_day(after_local + timedelta(days=delta_days), tz)
        if day.isoweekday() not in weekdays:
            continue
        candidate = day + offset
        if candidate > after_local:
            return candidate
    # 理论上不可达（weekdays 非空时 7 天内必有匹配）
    raise ValueError("无法计算每周调度的下次执行时间")


def _next_once(after_local: datetime, config: Dict[str, Any], tz: ZoneInfo) -> Optional[datetime]:
    """一次性：到点返回该时刻，已过期返回 None"""
    raw = config.get("run_at")
    if not raw:
        raise ValueError("once 类型必须提供 schedule_config.run_at")

    if isinstance(raw, datetime):
        moment = raw
    else:
        try:
            # 兼容 'Z' 结尾的 ISO8601
            moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"无法解析 run_at '{raw}'，应为 ISO8601 时间字符串")

    moment = _ensure_aware(moment, tz).astimezone(tz)
    if moment <= after_local:
        return None
    return moment


def _next_interval(after_local: datetime, config: Dict[str, Any]) -> datetime:
    """固定间隔：基准时刻 + 间隔小时数"""
    hours = config.get("interval_hours", DEFAULT_INTERVAL_HOURS)
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        raise ValueError(f"interval_hours 必须是数字，当前为 '{hours}'")
    if hours <= 0:
        raise ValueError("interval_hours 必须大于 0")
    return after_local + timedelta(hours=hours)


def compute_next_run(
    schedule_type: str,
    schedule_config: Optional[Dict[str, Any]],
    tz_name: Optional[str],
    after: datetime,
) -> Optional[datetime]:
    """计算下次执行时间

    Args:
        schedule_type: once / daily / weekly / interval / manual
        schedule_config: 调度参数（见模块文档）
        tz_name: 计划配置的时区名（如 "Asia/Shanghai"）
        after: 基准时刻（naive 时按 UTC 解释）

    Returns:
        UTC 时区感知的下次执行时间；manual 类型或 once 已过期时返回 None

    Raises:
        ValueError: 调度类型非法或调度参数不可解析
    """
    if schedule_type not in VALID_SCHEDULE_TYPES:
        raise ValueError(
            f"未知的调度类型 '{schedule_type}'，可选值: {', '.join(VALID_SCHEDULE_TYPES)}"
        )

    if schedule_type == MANUAL:
        return None

    config: Dict[str, Any] = schedule_config or {}
    tz = _get_timezone(tz_name)
    after_local = _ensure_aware(after, timezone.utc).astimezone(tz)

    if schedule_type == ONCE:
        candidate = _next_once(after_local, config, tz)
    elif schedule_type == INTERVAL:
        candidate = _next_interval(after_local, config)
    else:
        offset = _parse_clock_time(config.get("time"))
        if schedule_type == DAILY:
            candidate = _next_daily(after_local, offset, tz)
        else:
            candidate = _next_weekly(after_local, offset, _parse_weekdays(config.get("weekdays")), tz)

    if candidate is None:
        return None

    # 统一转 UTC 存库
    return candidate.astimezone(timezone.utc)


def describe_schedule(
    schedule_type: str,
    schedule_config: Optional[Dict[str, Any]],
    tz_name: Optional[str] = DEFAULT_TIMEZONE,
) -> str:
    """生成人类可读的调度描述（供前端列表展示）"""
    config: Dict[str, Any] = schedule_config or {}
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    if schedule_type == MANUAL:
        return "仅手动触发"

    if schedule_type == ONCE:
        raw = config.get("run_at")
        if not raw:
            return "一次性（未设置时间）"
        try:
            moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            moment = _ensure_aware(moment, _get_timezone(tz_name)).astimezone(_get_timezone(tz_name))
            return f"一次性 · {moment.strftime('%Y-%m-%d %H:%M')} ({tz_name})"
        except ValueError:
            return f"一次性 · {raw}"

    if schedule_type == INTERVAL:
        hours = config.get("interval_hours", DEFAULT_INTERVAL_HOURS)
        return f"每 {hours} 小时"

    time_str = config.get("time") or "00:00"
    if schedule_type == DAILY:
        return f"每天 {time_str} ({tz_name})"

    if schedule_type == WEEKLY:
        try:
            weekdays = _parse_weekdays(config.get("weekdays"))
        except ValueError:
            weekdays = []
        names = "、".join(weekday_names[d - 1] for d in weekdays) or "每天"
        return f"每周 {names} {time_str} ({tz_name})"

    return "未知调度方式"
