"""
调度器 next_run 纯函数测试

覆盖五种调度类型与跨日 / 跨月 / 跨年 / 跨时区（含 DST）/ 过期 once 等边界。
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.scheduler.next_run import compute_next_run, describe_schedule

SH = "Asia/Shanghai"
UTC = timezone.utc


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _sh(*args) -> datetime:
    """构造 Asia/Shanghai 本地时间（UTC+8，无 DST）"""
    return datetime(*args, tzinfo=timezone(timedelta(hours=8)))


# ============ daily ============

class TestDaily:
    def test_same_day_when_time_not_passed(self):
        result = compute_next_run("daily", {"time": "15:00"}, SH, _sh(2026, 9, 16, 10, 0))
        assert result == _utc(2026, 9, 16, 7, 0)

    def test_next_day_when_time_already_passed(self):
        result = compute_next_run("daily", {"time": "15:00"}, SH, _sh(2026, 9, 16, 16, 0))
        assert result == _utc(2026, 9, 17, 7, 0)

    def test_exactly_at_scheduled_time_rolls_to_next_day(self):
        """恰好等于触发时刻时视为已触发，顺延到次日（避免同一分钟内重复触发）"""
        result = compute_next_run("daily", {"time": "15:00"}, SH, _sh(2026, 9, 16, 15, 0))
        assert result == _utc(2026, 9, 17, 7, 0)

    def test_cross_year(self):
        result = compute_next_run("daily", {"time": "15:00"}, SH, _sh(2026, 12, 31, 23, 0))
        assert result == _utc(2027, 1, 1, 7, 0)

    def test_cross_month(self):
        result = compute_next_run("daily", {"time": "00:30"}, SH, _sh(2026, 9, 30, 23, 59))
        assert result == _utc(2026, 9, 30, 16, 30)

    def test_seconds_supported(self):
        result = compute_next_run("daily", {"time": "15:00:30"}, SH, _sh(2026, 9, 16, 10, 0))
        assert result == _utc(2026, 9, 16, 7, 0, 30)

    def test_result_is_utc_aware(self):
        result = compute_next_run("daily", {"time": "15:00"}, SH, _sh(2026, 9, 16, 10, 0))
        assert result.tzinfo is not None
        assert result.utcoffset() == timedelta(0)


# ============ weekly ============

class TestWeekly:
    def test_next_matching_weekday(self):
        # 2026-09-16 是周三，weekdays=[1] 表示周一 -> 下周一 09-21
        result = compute_next_run(
            "weekly", {"time": "15:00", "weekdays": [1]}, SH, _sh(2026, 9, 16, 10, 0)
        )
        assert result == _utc(2026, 9, 21, 7, 0)

    def test_today_matches_and_time_not_passed(self):
        # 周三 10:00，weekdays=[3] -> 当天 15:00
        result = compute_next_run(
            "weekly", {"time": "15:00", "weekdays": [3]}, SH, _sh(2026, 9, 16, 10, 0)
        )
        assert result == _utc(2026, 9, 16, 7, 0)

    def test_today_matches_but_time_passed(self):
        # 周三 16:00，weekdays=[3] -> 下周三 09-23
        result = compute_next_run(
            "weekly", {"time": "15:00", "weekdays": [3]}, SH, _sh(2026, 9, 16, 16, 0)
        )
        assert result == _utc(2026, 9, 23, 7, 0)

    def test_multiple_weekdays_picks_nearest(self):
        # 周三 10:00，weekdays=[5, 3] -> 当天（周三）15:00
        result = compute_next_run(
            "weekly", {"time": "15:00", "weekdays": [5, 3]}, SH, _sh(2026, 9, 16, 10, 0)
        )
        assert result == _utc(2026, 9, 16, 7, 0)

    def test_missing_weekdays_means_every_day(self):
        result = compute_next_run("weekly", {"time": "15:00"}, SH, _sh(2026, 9, 16, 10, 0))
        assert result == _utc(2026, 9, 16, 7, 0)

    def test_workdays_only(self):
        # 2026-09-19 是周六（isoweekday=6），weekdays=[1..5] -> 下周一 09-21 09:00
        result = compute_next_run(
            "weekly",
            {"time": "09:00", "weekdays": [1, 2, 3, 4, 5]},
            SH,
            _sh(2026, 9, 19, 8, 0),
        )
        assert result == _utc(2026, 9, 21, 1, 0)

    def test_weekend_is_excluded(self):
        # 周五 18:00 已过 09:00，weekdays=[1..5] -> 跳过整个周末到下周一
        result = compute_next_run(
            "weekly",
            {"time": "09:00", "weekdays": [1, 2, 3, 4, 5]},
            SH,
            _sh(2026, 9, 18, 18, 0),
        )
        assert result == _utc(2026, 9, 21, 1, 0)


# ============ once ============

class TestOnce:
    def test_future_run_at(self):
        result = compute_next_run(
            "once", {"run_at": "2026-09-17T15:00:00+08:00"}, SH, _utc(2026, 9, 16, 0, 0)
        )
        assert result == _utc(2026, 9, 17, 7, 0)

    def test_expired_returns_none(self):
        result = compute_next_run(
            "once", {"run_at": "2026-09-15T15:00:00+08:00"}, SH, _utc(2026, 9, 16, 0, 0)
        )
        assert result is None

    def test_naive_run_at_uses_schedule_timezone(self):
        result = compute_next_run(
            "once", {"run_at": "2026-09-17T15:00:00"}, SH, _utc(2026, 9, 16, 0, 0)
        )
        assert result == _utc(2026, 9, 17, 7, 0)

    def test_zulu_suffix_supported(self):
        result = compute_next_run(
            "once", {"run_at": "2026-09-17T07:00:00Z"}, SH, _utc(2026, 9, 16, 0, 0)
        )
        assert result == _utc(2026, 9, 17, 7, 0)

    def test_datetime_instance_accepted(self):
        result = compute_next_run(
            "once", {"run_at": _utc(2026, 9, 17, 7, 0)}, SH, _utc(2026, 9, 16, 0, 0)
        )
        assert result == _utc(2026, 9, 17, 7, 0)

    def test_missing_run_at_raises(self):
        with pytest.raises(ValueError):
            compute_next_run("once", {}, SH, _utc(2026, 9, 16))


# ============ interval ============

class TestInterval:
    def test_adds_interval_hours(self):
        result = compute_next_run(
            "interval", {"interval_hours": 6}, SH, _utc(2026, 9, 16, 0, 0)
        )
        assert result == _utc(2026, 9, 16, 6, 0)

    def test_default_interval_is_24h(self):
        result = compute_next_run("interval", {}, SH, _utc(2026, 9, 16, 0, 0))
        assert result == _utc(2026, 9, 17, 0, 0)

    def test_fractional_hours(self):
        result = compute_next_run(
            "interval", {"interval_hours": 0.5}, SH, _utc(2026, 9, 16, 0, 0)
        )
        assert result == _utc(2026, 9, 16, 0, 30)

    def test_non_positive_raises(self):
        with pytest.raises(ValueError):
            compute_next_run("interval", {"interval_hours": 0}, SH, _utc(2026, 9, 16))

    def test_non_numeric_raises(self):
        with pytest.raises(ValueError):
            compute_next_run("interval", {"interval_hours": "abc"}, SH, _utc(2026, 9, 16))


# ============ manual ============

def test_manual_never_schedules():
    assert compute_next_run("manual", {}, SH, _utc(2026, 9, 16)) is None
    assert compute_next_run("manual", None, None, _utc(2026, 9, 16)) is None


# ============ 参数与边界 ============

class TestArguments:
    def test_unknown_schedule_type_raises(self):
        with pytest.raises(ValueError):
            compute_next_run("cron", {"expr": "* * * * *"}, SH, _utc(2026, 9, 16))

    def test_invalid_time_format_raises(self):
        for bad in ("25:00", "aa:bb", "15", "", None):
            with pytest.raises(ValueError):
                compute_next_run("daily", {"time": bad}, SH, _utc(2026, 9, 16))

    def test_invalid_weekday_raises(self):
        with pytest.raises(ValueError):
            compute_next_run("weekly", {"time": "15:00", "weekdays": [0]}, SH, _utc(2026, 9, 16))
        with pytest.raises(ValueError):
            compute_next_run("weekly", {"time": "15:00", "weekdays": [8]}, SH, _utc(2026, 9, 16))
        with pytest.raises(ValueError):
            compute_next_run("weekly", {"time": "15:00", "weekdays": []}, SH, _utc(2026, 9, 16))

    def test_naive_after_is_interpreted_as_utc(self):
        naive = datetime(2026, 9, 16, 2, 0)  # 相当于 UTC 02:00 = 上海 10:00
        result = compute_next_run("daily", {"time": "15:00"}, SH, naive)
        assert result == _utc(2026, 9, 16, 7, 0)

    def test_unknown_timezone_falls_back_to_default(self):
        result = compute_next_run(
            "daily", {"time": "15:00"}, "Mars/Olympus", _utc(2026, 9, 16, 2, 0)
        )
        # 回退到 Asia/Shanghai
        assert result == _utc(2026, 9, 16, 7, 0)

    def test_none_timezone_uses_default(self):
        result = compute_next_run("daily", {"time": "15:00"}, None, _utc(2026, 9, 16, 2, 0))
        assert result == _utc(2026, 9, 16, 7, 0)


class TestDaylightSaving:
    def test_daily_across_spring_forward(self):
        """America/New_York 2026-03-08 进入夏令时：本地 15:00 对应的 UTC 时刻提前 1 小时"""
        tz = "America/New_York"
        before = compute_next_run("daily", {"time": "15:00"}, tz, _utc(2026, 3, 7, 12, 0))
        after = compute_next_run("daily", {"time": "15:00"}, tz, _utc(2026, 3, 8, 12, 0))

        assert before == _utc(2026, 3, 7, 20, 0)  # EST = UTC-5
        assert after == _utc(2026, 3, 8, 19, 0)   # EDT = UTC-4（当天本地 08:00，15:00 尚未到）

    def test_utc_timezone_has_no_offset_shift(self):
        result = compute_next_run("daily", {"time": "15:00"}, "UTC", _utc(2026, 9, 16, 10, 0))
        assert result == _utc(2026, 9, 16, 15, 0)


# ============ 描述文案 ============

class TestDescribeSchedule:
    def test_descriptions(self):
        assert describe_schedule("manual", {}, SH) == "仅手动触发"
        assert describe_schedule("daily", {"time": "15:00"}, SH) == f"每天 15:00 ({SH})"
        assert describe_schedule("interval", {"interval_hours": 6}, SH) == "每 6 小时"
        assert "周一" in describe_schedule("weekly", {"time": "15:00", "weekdays": [1]}, SH)
        assert "2026-09-17 15:00" in describe_schedule(
            "once", {"run_at": "2026-09-17T15:00:00+08:00"}, SH
        )

    def test_invalid_config_does_not_raise(self):
        assert isinstance(describe_schedule("weekly", {"time": "bad", "weekdays": [99]}, SH), str)
        assert isinstance(describe_schedule("unknown-type", {}, SH), str)
