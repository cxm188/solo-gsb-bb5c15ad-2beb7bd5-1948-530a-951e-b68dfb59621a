"""双探头温度偏差复核测试：切分插值、断档未知、不跨越合并、最短时长与 422 校验。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.probe_deviation import probe_deviation
from app.review import ReviewError

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 25, 10, 0, 0, tzinfo=CST)
client = TestClient(app)

PATH = "/api/v1/cold-storage/probes/deviation-review"


def at(second: float) -> datetime:
    return T0 + timedelta(seconds=second)


def rel(ivs) -> list[tuple[float, float]]:
    """把区间转成相对 T0 的 (起点秒, 终点秒)。"""
    base = T0.timestamp()
    return [(round(iv.start - base, 3), round(iv.end - base, 3)) for iv in ivs]


def run(
    primary,
    backup,
    gap_a=120.0,
    gap_b=120.0,
    delta=1.0,
    min_dev=0.0,
):
    return probe_deviation(
        primary=[(at(t), temp) for t, temp in primary],
        backup=[(at(t), temp) for t, temp in backup],
        max_sample_gap_seconds_primary=gap_a,
        max_sample_gap_seconds_backup=gap_b,
        allowed_delta=delta,
        min_deviation_seconds=min_dev,
    )


# ---------------------------------------------------------------------------
# 算法层
# ---------------------------------------------------------------------------


def test_constant_deviation_over_threshold():
    # 主 -17、备 -18.5：绝对温差 1.5 > 1，整段偏差 [0, 120]。
    result = run(
        [(0, -17.0), (120, -17.0)],
        [(0, -18.5), (120, -18.5)],
    )
    assert rel(result.deviation_intervals) == [(0.0, 120.0)]
    assert result.deviation_seconds == pytest.approx(120.0)
    assert result.unknown_intervals == []


def test_deviation_equal_to_allowed_is_not_deviation():
    # 绝对温差恰好等于允许值：严格大于才算，无偏差。
    result = run(
        [(0, -17.0), (120, -17.0)],
        [(0, -18.0), (120, -18.0)],
        delta=1.0,
    )
    assert result.deviation_intervals == []
    assert result.deviation_seconds == 0.0


def test_linear_crossing_splits_at_interpolated_point():
    # 主恒温 -17，备从 -18 升到 -17：差值 1 -> 0，允许 0.5，
    # d(t) = 1 - t/120，|d| = 0.5 的穿越点在 60s，偏差区间 [0, 60]。
    result = run(
        [(0, -17.0), (120, -17.0)],
        [(0, -18.0), (120, -17.0)],
        delta=0.5,
    )
    assert rel(result.deviation_intervals) == [(0.0, 60.0)]


def test_abs_value_two_tails_both_sides_over():
    # 主恒温 -18，备从 -17 线性降到 -19：差值 d 从 +1 到 -1，允许 0.5。
    # d(t) = 1 - t/60：d=0.5 在 30s，d=-0.5 在 90s；中段 |d|<0.5 不断开合并。
    result = run(
        [(0, -18.0), (120, -18.0)],
        [(0, -17.0), (120, -19.0)],
        delta=0.5,
    )
    assert rel(result.deviation_intervals) == [(0.0, 30.0), (90.0, 120.0)]


def test_axis_split_at_both_sides_sample_times():
    # 两侧采样时刻交错：主在 0/60/120，备在 0/30/90/120，温差恒超标，
    # 结果仍是一整段（公共断点严格超标时合并）。
    result = run(
        [(0, -17.0), (60, -17.0), (120, -17.0)],
        [(0, -19.0), (30, -19.0), (90, -19.0), (120, -19.0)],
    )
    assert rel(result.deviation_intervals) == [(0.0, 120.0)]


def test_segments_do_not_merge_at_boundary_equal_to_allowed():
    # 公共采样时刻 60s 绝对温差恰好等于允许值：两侧偏差几何上相接也不合并。
    # 备恒温 -18；主 -16 -> -17 -> -16，差值 d: 2 -> 1 -> 2，允许 1。
    # [0,60) 与 (60,120] 严格超标，t=60 本身不超标（半开区间两段断开）。
    result = run(
        [(0, -16.0), (60, -17.0), (120, -16.0)],
        [(0, -18.0), (60, -18.0), (120, -18.0)],
        delta=1.0,
    )
    ivs = rel(result.deviation_intervals)
    assert ivs == [(0.0, 60.0), (60.0, 120.0)]


def test_min_deviation_duration_filters_and_boundary_inclusive():
    result = run(
        [(0, -17.0), (120, -17.0)],
        [(0, -18.0), (120, -17.0)],
        delta=0.5,
        min_dev=60.0,
    )
    # 偏差区间恰为 60s：阈值含等号，保留。
    assert rel(result.deviation_intervals) == [(0.0, 60.0)]
    result = run(
        [(0, -17.0), (120, -17.0)],
        [(0, -18.0), (120, -17.0)],
        delta=0.5,
        min_dev=61.0,
    )
    assert result.deviation_intervals == []
    assert result.deviation_seconds == 0.0


def test_primary_gap_is_unknown_and_not_crossed():
    # 主探头断档 [60, 300]：即使两侧偏差都超标，也不跨越未知区间合并。
    result = run(
        [(0, -17.0), (60, -17.0), (300, -17.0), (360, -17.0)],
        [(0, -19.0), (360, -19.0)],
        gap_a=120.0,
        gap_b=1000.0,
        delta=1.0,
    )
    assert rel(result.deviation_intervals) == [(0.0, 60.0), (300.0, 360.0)]
    assert rel(result.unknown_intervals) == [(60.0, 300.0)]
    assert result.unknown_seconds == pytest.approx(240.0)


def test_backup_gap_is_unknown():
    result = run(
        [(0, -17.0), (360, -17.0)],
        [(0, -19.0), (60, -19.0), (300, -19.0), (360, -19.0)],
        gap_a=1000.0,
        gap_b=120.0,
        delta=1.0,
    )
    assert rel(result.unknown_intervals) == [(60.0, 300.0)]
    assert rel(result.deviation_intervals) == [(0.0, 60.0), (300.0, 360.0)]


def test_gap_exactly_at_threshold_is_allowed():
    # 间隔恰好等于阈值：不断档，正常插值。
    result = run(
        [(0, -17.0), (120, -17.0)],
        [(0, -19.0), (120, -19.0)],
        gap_a=120.0,
        gap_b=120.0,
    )
    assert result.unknown_intervals == []
    assert rel(result.deviation_intervals) == [(0.0, 120.0)]


def test_only_overlap_intersection_is_evaluated_no_extrapolation():
    # 主覆盖 [0, 300]，备覆盖 [60, 240]：仅在交集 [60, 240] 内核算，不外推。
    result = run(
        [(0, -17.0), (300, -17.0)],
        [(60, -19.0), (240, -19.0)],
        gap_a=1000.0,
        gap_b=1000.0,
    )
    assert rel(result.deviation_intervals) == [(60.0, 240.0)]
    assert result.unknown_intervals == []


def test_intersection_endpoint_inside_gap_counts_unknown_clipped():
    # 交集结束于备探头的断档段内部：交集内的断档部分标为未知。
    result = run(
        [(0, -17.0), (360, -17.0)],
        [(0, -19.0), (60, -19.0), (300, -19.0)],
        gap_a=1000.0,
        gap_b=120.0,
    )
    assert rel(result.unknown_intervals) == [(60.0, 300.0)]
    assert rel(result.deviation_intervals) == [(0.0, 60.0)]


def test_deviation_resumes_without_merging_after_unknown():
    # 未知两侧偏差区间相接于未知边界，但绝不合并为一个区间。
    result = run(
        [(0, -17.0), (60, -17.0), (180, -17.0), (240, -17.0)],
        [(0, -19.0), (240, -19.0)],
        gap_a=100.0,
        gap_b=1000.0,
    )
    assert rel(result.unknown_intervals) == [(60.0, 180.0)]
    assert len(result.deviation_intervals) == 2


def test_no_overlap_rejected():
    with pytest.raises(ReviewError):
        run(
            [(0, -17.0), (60, -17.0)],
            [(120, -19.0), (180, -19.0)],
        )


def test_endpoint_only_overlap_rejected():
    # 仅端点相接（交集为空）：返回 422。
    with pytest.raises(ReviewError):
        run(
            [(0, -17.0), (60, -17.0)],
            [(60, -19.0), (120, -19.0)],
        )


def test_invalid_parameters_rejected():
    good_a = [(0, -17.0), (60, -17.0)]
    good_b = [(0, -19.0), (60, -19.0)]
    with pytest.raises(ReviewError):
        run(good_a, good_b, gap_a=0)
    with pytest.raises(ReviewError):
        run(good_a, good_b, gap_b=-1)
    with pytest.raises(ReviewError):
        run(good_a, good_b, delta=-0.1)
    with pytest.raises(ReviewError):
        run(good_a, good_b, delta=float("nan"))
    with pytest.raises(ReviewError):
        run(good_a, good_b, min_dev=-1)
    with pytest.raises(ReviewError):
        probe_deviation(
            primary=[(at(0), float("nan")), (at(60), -17.0)],
            backup=good_b,
            max_sample_gap_seconds_primary=120,
            max_sample_gap_seconds_backup=120,
            allowed_delta=1.0,
            min_deviation_seconds=0.0,
        )
    with pytest.raises(ReviewError):
        probe_deviation(
            primary=[(at(0), -17.0)],
            backup=good_b,
            max_sample_gap_seconds_primary=120,
            max_sample_gap_seconds_backup=120,
            allowed_delta=1.0,
            min_deviation_seconds=0.0,
        )


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        probe_deviation(
            primary=[(datetime(2026, 9, 25, 10, 0, 0), -17.0), (at(60), -17.0)],
            backup=[(at(0), -19.0), (at(60), -19.0)],
            max_sample_gap_seconds_primary=120,
            max_sample_gap_seconds_backup=120,
            allowed_delta=1.0,
            min_deviation_seconds=0.0,
        )


def test_different_timezones_are_normalized():
    # 主用 +08:00，备用 UTC：同一绝对时刻的读数仍对齐。
    utc = timezone.utc
    result = probe_deviation(
        primary=[(at(0), -17.0), (at(120), -17.0)],
        backup=[
            (datetime(2026, 9, 25, 2, 0, 0, tzinfo=utc), -19.0),
            (datetime(2026, 9, 25, 2, 2, 0, tzinfo=utc), -19.0),
        ],
        max_sample_gap_seconds_primary=120,
        max_sample_gap_seconds_backup=120,
        allowed_delta=1.0,
        min_deviation_seconds=0.0,
    )
    assert rel(result.deviation_intervals) == [(0.0, 120.0)]


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------


def iso(hour: int, minute: int = 0, second: int = 0) -> str:
    return datetime(2026, 9, 25, hour, minute, second, tzinfo=CST).isoformat()


def base_payload(**overrides):
    payload = {
        "primary": {
            "readings": [
                {"time": iso(10, 0, 0), "temperature": -17.0},
                {"time": iso(10, 1, 0), "temperature": -17.0},
                {"time": iso(10, 2, 0), "temperature": -17.0},
            ],
            "max_sample_gap_seconds": 120,
        },
        "backup": {
            "readings": [
                {"time": iso(10, 0, 0), "temperature": -19.0},
                {"time": iso(10, 2, 0), "temperature": -19.0},
            ],
            "max_sample_gap_seconds": 300,
        },
        "allowed_delta": 1.0,
        "min_deviation_seconds": 60,
    }
    payload.update(overrides)
    return payload


def test_api_end_to_end():
    resp = client.post(PATH, json=base_payload())
    assert resp.status_code == 200
    data = resp.json()
    assert data["deviation_count"] == 1
    iv = data["deviation_intervals"][0]
    assert iv["start"].endswith("Z")  # 输出统一 UTC：10:00 CST -> 02:00 UTC
    assert iv["start"].startswith("2026-09-25T02:00:00")
    assert iv["end"].startswith("2026-09-25T02:02:00")
    assert iv["duration_seconds"] == 120.0
    assert data["deviation_seconds"] == 120.0
    assert data["unknown_count"] == 0
    assert data["unknown_seconds"] == 0.0


def test_api_unknown_gap_does_not_merge():
    payload = base_payload(
        primary={
            "readings": [
                {"time": iso(10, 0, 0), "temperature": -17.0},
                {"time": iso(10, 1, 0), "temperature": -17.0},
                {"time": iso(10, 10, 0), "temperature": -17.0},
                {"time": iso(10, 11, 0), "temperature": -17.0},
            ],
            "max_sample_gap_seconds": 120,
        },
        backup={
            "readings": [
                {"time": iso(10, 0, 0), "temperature": -19.0},
                {"time": iso(10, 11, 0), "temperature": -19.0},
            ],
            "max_sample_gap_seconds": 1000,
        },
        min_deviation_seconds=30,
    )
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["deviation_count"] == 2
    assert data["unknown_count"] == 1
    assert data["unknown_intervals"][0]["duration_seconds"] == 540.0
    assert data["unknown_seconds"] == 540.0


def test_api_no_overlap_returns_422():
    payload = base_payload(
        backup={
            "readings": [
                {"time": iso(10, 20, 0), "temperature": -19.0},
                {"time": iso(10, 21, 0), "temperature": -19.0},
            ],
            "max_sample_gap_seconds": 120,
        }
    )
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 422
    assert "交集" in resp.text


def test_api_endpoint_only_touch_returns_422():
    payload = base_payload(
        backup={
            "readings": [
                {"time": iso(10, 2, 0), "temperature": -19.0},
                {"time": iso(10, 3, 0), "temperature": -19.0},
            ],
            "max_sample_gap_seconds": 120,
        }
    )
    assert client.post(PATH, json=payload).status_code == 422


def test_api_non_increasing_readings_returns_422():
    payload = base_payload(
        primary={
            "readings": [
                {"time": iso(10, 1, 0), "temperature": -17.0},
                {"time": iso(10, 0, 0), "temperature": -17.0},
            ],
            "max_sample_gap_seconds": 120,
        }
    )
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 422
    assert "严格递增" in resp.text


def test_api_naive_time_returns_422():
    payload = base_payload()
    payload["backup"]["readings"][0]["time"] = "2026-09-25T10:00:00"
    assert client.post(PATH, json=payload).status_code == 422


def test_api_bad_parameters_return_422():
    assert client.post(PATH, json=base_payload(allowed_delta=-1)).status_code == 422
    assert client.post(PATH, json=base_payload(min_deviation_seconds=-5)).status_code == 422
    payload = base_payload()
    payload["primary"]["max_sample_gap_seconds"] = 0
    assert client.post(PATH, json=payload).status_code == 422
    payload = base_payload(
        primary={
            "readings": [
                {"time": iso(10, 0, 0), "temperature": -17.0},
            ],
            "max_sample_gap_seconds": 120,
        }
    )
    assert client.post(PATH, json=payload).status_code == 422


def test_schema_rejects_non_finite_values():
    # NaN/Infinity 不是合法 JSON 数值，直接在模型层验证字段被拒绝。
    from pydantic import ValidationError

    from app.schemas import ProbeDeviationRequest

    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            ProbeDeviationRequest(**base_payload(allowed_delta=bad))
        with pytest.raises(ValidationError):
            ProbeDeviationRequest(**base_payload(min_deviation_seconds=bad))
