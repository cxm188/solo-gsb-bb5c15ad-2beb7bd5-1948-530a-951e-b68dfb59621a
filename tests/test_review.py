"""算法层测试：线性插值、断档、豁免扣减、时长筛选。

温度上限固定 -18°C：高于上限（如 -17°C）为越限，低于上限（如 -19°C）安全。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import review as rl
from app.review import ReviewError, review

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 25, 10, 0, 0, tzinfo=CST)


def at(second: float) -> datetime:
    return T0 + timedelta(seconds=second)


def rel(ivs) -> list[tuple[float, float]]:
    """把区间转成相对 T0 的 (起点秒, 终点秒)。"""
    base = T0.timestamp()
    return [(round(iv.start - base, 3), round(iv.end - base, 3)) for iv in ivs]


def run(readings, limit=-18.0, gap=120.0, min_alarm=30.0, exemptions=None):
    return review(
        readings=[(at(t), temp) for t, temp in readings],
        limit=limit,
        max_sample_gap_seconds=gap,
        min_alarm_seconds=min_alarm,
        exemptions=[(at(s), at(e)) for s, e in exemptions or []],
    )


def test_basic_crossing_with_linear_interpolation():
    # 0s 安全(-19) -> 60s 越限(-17)：穿越点在中点 30s 处，越限区间 [30,60]。
    result = run([(0, -19.0), (60, -17.0)], min_alarm=0.0)
    assert rel(result.exceed_intervals) == [(30.0, 60.0)]
    assert rel(result.alarm_intervals) == [(30.0, 60.0)]
    assert result.unknown_intervals == []


def test_value_equal_to_limit_endpoint_is_not_over():
    # -18(等于上限) -> -17(越限)：穿越点就在端点 t=0，越限区间 (0,60] ≈ [0,60]。
    result = run([(0, -18.0), (60, -17.0)], min_alarm=0.0)
    assert rel(result.exceed_intervals) == [(0.0, 60.0)]


def test_over_segments_merge_at_strictly_over_point():
    # 公共点 60s 温度 -17（严格越限）：两段 [30,60]、[60,90] 合并为 [30,90]。
    result = run(
        [(0, -19.0), (60, -17.0), (120, -19.0)], min_alarm=0.0
    )
    assert rel(result.exceed_intervals) == [(30.0, 90.0)]


def test_touching_at_limit_does_not_merge():
    # 0..60 从越限(-17)降到等于上限(-18)，60..120 再升回越限(-17)。
    # 公共点 60s 温度等于上限（不越限），两段必须断开。
    result = run([(0, -17.0), (60, -18.0), (120, -17.0)], min_alarm=0.0)
    assert rel(result.exceed_intervals) == [(0.0, 60.0), (60.0, 120.0)]


def test_sample_gap_is_unknown_and_not_crossed():
    # 断档两侧都越限，也不能跨越断档合并。
    result = run(
        [(0, -17.0), (60, -17.0), (300, -17.0), (360, -17.0)],
        gap=120.0,
        min_alarm=0.0,
    )
    assert rel(result.exceed_intervals) == [(0.0, 60.0), (300.0, 360.0)]
    assert rel(result.unknown_intervals) == [(60.0, 300.0)]


def test_gap_exactly_at_limit_is_allowed():
    # 间隔恰好等于最大允许值：不视为断档。
    result = run([(0, -17.0), (120, -17.0)], gap=120.0, min_alarm=0.0)
    assert result.unknown_intervals == []
    assert rel(result.exceed_intervals) == [(0.0, 120.0)]


def test_maintenance_exemption_subtracted():
    # 越限区间 [0, 120]，豁免 [30, 90]：剩余两段各 30s。
    result = run(
        [(0, -17.0), (120, -17.0)],
        exemptions=[(30, 90)],
        min_alarm=30.0,
    )
    assert rel(result.exceed_intervals) == [(0.0, 120.0)]
    assert rel(result.alarm_intervals) == [(0.0, 30.0), (90.0, 120.0)]


def test_min_alarm_duration_filters_short_segments():
    # 豁免后各段为 10s、50s；最短报警 30s，只保留长段。
    result = run(
        [(0, -17.0), (120, -17.0)],
        exemptions=[(10, 70)],
        min_alarm=30.0,
    )
    assert rel(result.alarm_intervals) == [(70.0, 120.0)]


def test_min_alarm_boundary_is_inclusive():
    result = run([(0, -17.0), (30, -17.0)], min_alarm=30.0)
    assert rel(result.alarm_intervals) == [(0.0, 30.0)]


def test_exemption_covers_all_means_no_alarm():
    result = run(
        [(0, -17.0), (120, -17.0)],
        exemptions=[(0, 120)],
        min_alarm=0.0,
    )
    assert result.exceed_intervals  # 原始越限仍然可见
    assert result.alarm_intervals == []


def test_exemption_partial_edges():
    # 豁免与越限区间部分重叠（区间外的部分应被裁剪）。
    result = run(
        [(0, -17.0), (120, -17.0)],
        exemptions=[(-60, 20), (100, 200)],
        min_alarm=0.0,
    )
    assert rel(result.alarm_intervals) == [(20.0, 100.0)]


def test_overlapping_exemptions_are_merged():
    result = run(
        [(0, -17.0), (120, -17.0)],
        exemptions=[(10, 60), (40, 110)],
        min_alarm=0.0,
    )
    assert rel(result.alarm_intervals) == [(0.0, 10.0), (110.0, 120.0)]


def test_no_exceedance():
    result = run([(0, -19.0), (60, -20.0)])
    assert result.exceed_intervals == []
    assert result.alarm_intervals == []


def test_utc_normalization_in_output():
    # 输入 +08:00，输出统一转 UTC（10:00 CST == 02:00 UTC）。
    result = run([(0, -17.0), (60, -17.0)], min_alarm=0.0)
    iv = result.exceed_intervals[0]
    assert rl.to_utc(iv.start) == datetime(2026, 9, 25, 2, 0, 0, tzinfo=timezone.utc)


def test_invalid_exemption_window_rejected():
    with pytest.raises(ReviewError):
        run([(0, -19.0), (60, -19.0)], exemptions=[(100, 100)])


def test_non_finite_temperature_rejected():
    with pytest.raises(ReviewError):
        run([(0, float("nan")), (60, -19.0)])


def test_too_few_readings_rejected():
    with pytest.raises(ReviewError):
        review(
            readings=[(at(0), -19.0)],
            limit=-18.0,
            max_sample_gap_seconds=120.0,
            min_alarm_seconds=0.0,
        )


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        review(
            readings=[
                (datetime(2026, 9, 25, 10, 0, 0), -19.0),
                (datetime(2026, 9, 25, 10, 1, 0), -19.0),
            ],
            limit=-18.0,
            max_sample_gap_seconds=120.0,
            min_alarm_seconds=0.0,
        )


def test_bad_parameters_rejected():
    kwargs = dict(
        readings=[(at(0), -19.0), (at(60), -19.0)],
        limit=-18.0,
        max_sample_gap_seconds=120.0,
        min_alarm_seconds=0.0,
    )
    with pytest.raises(ReviewError):
        review(**{**kwargs, "max_sample_gap_seconds": 0})
    with pytest.raises(ReviewError):
        review(**{**kwargs, "min_alarm_seconds": -1})
    with pytest.raises(ReviewError):
        review(**{**kwargs, "limit": float("inf")})
