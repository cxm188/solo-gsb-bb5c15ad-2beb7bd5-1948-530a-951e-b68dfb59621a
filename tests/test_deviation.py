"""双探头温度偏差复核：算法层与 API 层测试。

约定：主探头 A 与备探头 B 温度差值决定偏差；允许温差 tolerance，
``abs(T_A − T_B)`` 严格大于 tolerance 才算偏差。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.deviation import deviation_review
from app.main import app
from app.review import ReviewError

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 25, 10, 0, 0, tzinfo=CST)

client = TestClient(app)
PATH = "/api/v1/cold-storage/probe-deviation/review"


def at(second: float) -> datetime:
    return T0 + timedelta(seconds=second)


def rel(intervals) -> list[tuple[float, float]]:
    base = T0.timestamp()
    return [
        (round(iv.start - base, 3), round(iv.end - base, 3)) for iv in intervals
    ]


def run(primary, backup, pgap=300.0, bgap=300.0, tol=1.0, min_dev=0.0):
    return deviation_review(
        primary_readings=[(at(t), temp) for t, temp in primary],
        backup_readings=[(at(t), temp) for t, temp in backup],
        primary_max_sample_gap_seconds=pgap,
        backup_max_sample_gap_seconds=bgap,
        allowed_temperature_difference=tol,
        min_deviation_seconds=min_dev,
    )


# ---------------------------------------------------------------------------
# 算法层
# ---------------------------------------------------------------------------


def test_constant_difference_over_threshold():
    # 恒温差 2°C，允许 1°C：整段偏差。
    result = run([(0, -15.0), (120, -15.0)], [(0, -17.0), (120, -17.0)])
    assert rel(result.deviation_intervals) == [(0.0, 120.0)]
    assert result.unknown_intervals == []
    assert result.deviation_seconds == pytest.approx(120.0)
    assert result.unknown_seconds == 0.0


def test_difference_equal_to_threshold_is_not_deviation():
    # 温差恰好等于允许值：不算偏差。
    result = run([(0, -15.0), (60, -15.0)], [(0, -16.0), (60, -16.0)], tol=1.0)
    assert result.deviation_intervals == []
    assert result.deviation_seconds == 0.0


def test_ramp_crossing_solved_by_interpolation():
    # A 从 -16 线性升到 -14，B 恒温 -15，允许温差 0.5：
    # 温差 d 从 -1 升到 +1，|d|>0.5 为 [0,30) 与 (90,120]，穿越点插值求出。
    result = run(
        [(0, -16.0), (120, -14.0)], [(0, -15.0), (120, -15.0)], tol=0.5
    )
    assert rel(result.deviation_intervals) == [(0.0, 30.0), (90.0, 120.0)]
    assert result.deviation_seconds == pytest.approx(60.0)


def test_merged_deviation_across_shared_sample_point():
    # B 在 60s 有一个采样切点，但两侧温差都超阈：小段应合并为连续区间。
    result = run(
        [(0, -15.0), (120, -15.0)],
        [(0, -17.0), (60, -17.0), (120, -17.0)],
    )
    assert rel(result.deviation_intervals) == [(0.0, 120.0)]


def test_touching_at_threshold_does_not_merge():
    # A 恒温 -16，B 0s=-14.5 线性降到 120s=-17.5；温差 d=A-B 从 -1.5 升到 +1.5，
    # tol=1：d=-1 在 t=20、d=+1 在 t=100，偏差 [0,20) 与 (100,120]，中间不偏差。
    result = run(
        [(0, -16.0), (120, -16.0)], [(0, -14.5), (120, -17.5)], tol=1.0
    )
    assert rel(result.deviation_intervals) == [(0.0, 20.0), (100.0, 120.0)]


def test_gap_on_one_side_is_unknown_and_not_crossed():
    # 备探头 60->300 断档（240s）：偏差区间断开，交集内 240s 为未知。
    result = run(
        [(t, -15.0) for t in (0, 60, 120, 300, 360)],
        [(0, -17.0), (60, -17.0), (300, -17.0), (360, -17.0)],
        pgap=1000.0,
        bgap=120.0,
    )
    assert rel(result.deviation_intervals) == [(0.0, 60.0), (300.0, 360.0)]
    assert rel(result.unknown_intervals) == [(60.0, 300.0)]
    assert result.unknown_seconds == pytest.approx(240.0)


def test_each_side_uses_own_gap_threshold():
    # 主探头阈值 600（480s 间隔不断），备探头阈值 120（480s 断）。
    result = run(
        [(0, -15.0), (120, -15.0), (600, -15.0), (660, -15.0)],
        [(0, -17.0), (120, -17.0), (600, -17.0), (660, -17.0)],
        pgap=600.0,
        bgap=120.0,
    )
    assert rel(result.deviation_intervals) == [(0.0, 120.0), (600.0, 660.0)]
    assert rel(result.unknown_intervals) == [(120.0, 600.0)]


def test_gap_exactly_at_threshold_is_interpolated():
    result = run(
        [(0, -15.0), (120, -15.0)], [(0, -17.0), (120, -17.0)], pgap=120, bgap=120
    )
    assert result.unknown_intervals == []
    assert rel(result.deviation_intervals) == [(0.0, 120.0)]


def test_intersection_clip_and_no_extrapolation():
    # A 覆盖 [0,300]，B 覆盖 [60,240]：仅在交集 [60,240] 内分析，两端不外推。
    result = run(
        [(0, -15.0), (300, -15.0)], [(60, -17.0), (240, -17.0)]
    )
    assert rel(result.deviation_intervals) == [(60.0, 240.0)]
    assert result.deviation_seconds == pytest.approx(180.0)


def test_min_deviation_duration_filters():
    # 偏差 [0,30) 与 (90,120] 各 30s：最短 40s 全部过滤；最短 30s 全部保留。
    result = run(
        [(0, -16.0), (120, -14.0)],
        [(0, -15.0), (120, -15.0)],
        tol=0.5,
        min_dev=40.0,
    )
    assert result.deviation_intervals == []
    assert result.deviation_seconds == 0.0  # 被过滤区间不计入总时长

    result = run(
        [(0, -16.0), (120, -14.0)],
        [(0, -15.0), (120, -15.0)],
        tol=0.5,
        min_dev=30.0,
    )
    assert len(result.deviation_intervals) == 2


def test_no_overlap_rejected():
    with pytest.raises(ReviewError):
        run(
            [(0, -15.0), (60, -15.0)],
            [(120, -16.0), (180, -16.0)],
        )


def test_endpoint_only_touch_is_empty_intersection():
    # A 恰在 60s 结束、B 恰在 60s 开始：交集为空。
    with pytest.raises(ReviewError):
        run(
            [(0, -15.0), (60, -15.0)],
            [(60, -16.0), (120, -16.0)],
        )


def test_invalid_parameters_rejected():
    good_a = [(at(0), -15.0), (at(60), -15.0)]
    good_b = [(at(0), -16.0), (at(60), -16.0)]
    with pytest.raises(ReviewError):
        deviation_review(good_a, good_b, 300, 300, -0.1, 0)
    with pytest.raises(ReviewError):
        deviation_review(good_a, good_b, 300, 300, float("nan"), 0)
    with pytest.raises(ReviewError):
        deviation_review(good_a, good_b, 300, 300, 1.0, -1)
    with pytest.raises(ReviewError):
        deviation_review(good_a, good_b, 0, 300, 1.0, 0)


def test_reading_validation_reused():
    # 非递增 / NaN 温度 / 单条读数：复用 review.prepare_readings 的校验。
    with pytest.raises((ReviewError, ValueError)):
        deviation_review(
            [(at(60), -15.0), (at(0), -15.0)],
            [(at(0), -16.0), (at(60), -16.0)],
            300, 300, 1.0, 0,
        )
    with pytest.raises(ReviewError):
        deviation_review(
            [(at(0), float("nan")), (at(60), -15.0)],
            [(at(0), -16.0), (at(60), -16.0)],
            300, 300, 1.0, 0,
        )
    with pytest.raises(ValueError):
        deviation_review(
            [(datetime(2026, 9, 25, 10, 0, 0), -15.0), (at(60), -15.0)],
            [(at(0), -16.0), (at(60), -16.0)],
            300, 300, 1.0, 0,
        )


def test_unknown_intervals_are_merged():
    # 主探头在 [60,300] 整段断档，但备探头在 120/240 处采样把该段切成多个
    # 相邻小段：输出时合并为一个未知区间。
    result = run(
        [(0, -15.0), (60, -15.0), (300, -15.0), (360, -15.0)],
        [
            (0, -17.0),
            (60, -17.0),
            (120, -17.0),
            (240, -17.0),
            (300, -17.0),
            (360, -17.0),
        ],
        pgap=120.0,
        bgap=1000.0,
    )
    assert rel(result.unknown_intervals) == [(60.0, 300.0)]
    assert result.unknown_seconds == pytest.approx(240.0)


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------


def iso(hour: int, minute: int = 0, second: int = 0) -> str:
    return datetime(2026, 9, 25, hour, minute, second, tzinfo=CST).isoformat()


def payload(**overrides):
    p = {
        "primary_readings": [
            {"time": iso(10, 0, 0), "temperature": -15.0},
            {"time": iso(10, 1, 0), "temperature": -15.0},
            {"time": iso(10, 2, 0), "temperature": -15.0},
            {"time": iso(10, 10, 0), "temperature": -15.0},
            {"time": iso(10, 11, 0), "temperature": -15.0},
        ],
        "backup_readings": [
            {"time": iso(10, 0, 0), "temperature": -17.0},
            {"time": iso(10, 2, 0), "temperature": -17.0},
            {"time": iso(10, 10, 0), "temperature": -17.0},
            {"time": iso(10, 11, 0), "temperature": -17.0},
        ],
        "primary_max_sample_gap_seconds": 120,
        "backup_max_sample_gap_seconds": 120,
        "allowed_temperature_difference": 1.0,
        "min_deviation_seconds": 30,
    }
    p.update(overrides)
    return p


def test_api_end_to_end():
    resp = client.post(PATH, json=payload())
    assert resp.status_code == 200
    data = resp.json()
    assert data["deviation_count"] == 2
    assert data["unknown_count"] == 1
    assert [d["duration_seconds"] for d in data["deviation_intervals"]] == [120.0, 60.0]
    assert data["unknown_intervals"][0]["duration_seconds"] == 480.0
    assert data["deviation_seconds"] == 180.0
    assert data["unknown_seconds"] == 480.0
    # 时间统一 UTC：10:00 CST -> 02:00 UTC。
    assert data["deviation_intervals"][0]["start"] == "2026-09-25T02:00:00Z"
    assert all(d["start"].endswith("Z") for d in data["deviation_intervals"])
    assert all(u["end"].endswith("Z") for u in data["unknown_intervals"])


def test_api_422_on_no_overlap():
    resp = client.post(
        PATH,
        json=payload(
            primary_readings=[
                {"time": iso(10, 0, 0), "temperature": -15.0},
                {"time": iso(10, 1, 0), "temperature": -15.0},
            ],
            backup_readings=[
                {"time": iso(10, 2, 0), "temperature": -16.0},
                {"time": iso(10, 3, 0), "temperature": -16.0},
            ],
        ),
    )
    assert resp.status_code == 422
    assert "交集" in resp.text


def test_api_422_on_endpoint_touch_only():
    resp = client.post(
        PATH,
        json=payload(
            primary_readings=[
                {"time": iso(10, 0, 0), "temperature": -15.0},
                {"time": iso(10, 1, 0), "temperature": -15.0},
            ],
            backup_readings=[
                {"time": iso(10, 1, 0), "temperature": -16.0},
                {"time": iso(10, 2, 0), "temperature": -16.0},
            ],
        ),
    )
    assert resp.status_code == 422


def test_api_422_non_increasing_readings():
    resp = client.post(
        PATH,
        json=payload(
            backup_readings=[
                {"time": iso(10, 2, 0), "temperature": -17.0},
                {"time": iso(10, 0, 0), "temperature": -17.0},
            ]
        ),
    )
    assert resp.status_code == 422
    assert "严格递增" in resp.text


def test_api_422_naive_time():
    bad = payload()
    bad["primary_readings"][0]["time"] = "2026-09-25T10:00:00"
    resp = client.post(PATH, json=bad)
    assert resp.status_code == 422


def test_api_422_bad_parameters():
    assert client.post(PATH, json=payload(primary_max_sample_gap_seconds=0)).status_code == 422
    assert client.post(PATH, json=payload(backup_max_sample_gap_seconds=-5)).status_code == 422
    assert client.post(PATH, json=payload(allowed_temperature_difference=-1)).status_code == 422
    assert client.post(PATH, json=payload(min_deviation_seconds=-1)).status_code == 422


def test_api_422_single_reading():
    one = [{"time": iso(10, 0, 0), "temperature": -15.0}]
    assert client.post(PATH, json=payload(primary_readings=one)).status_code == 422


def test_api_422_non_finite_difference():
    # 标准 JSON 不允许 Infinity/NaN，用裸请求体发送；统一 422 处理器必须能渲染。
    template = (
        '{"primary_readings":['
        '{"time":"2026-09-25T10:00:00+08:00","temperature":-15.0},'
        '{"time":"2026-09-25T10:01:00+08:00","temperature":-15.0}],'
        '"backup_readings":['
        '{"time":"2026-09-25T10:00:00+08:00","temperature":-16.0},'
        '{"time":"2026-09-25T10:01:00+08:00","temperature":-16.0}],'
        '"primary_max_sample_gap_seconds":120,'
        '"backup_max_sample_gap_seconds":120,'
        '"allowed_temperature_difference":%s,"min_deviation_seconds":0}'
    )
    for raw_value in ("Infinity", "-Infinity", "NaN", "1e999"):
        resp = client.post(
            PATH,
            content=template % raw_value,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422, raw_value


def test_api_interpolated_crossing():
    # 与算法层 ramp 用例对应的 API 版本。
    resp = client.post(
        PATH,
        json=payload(
            primary_readings=[
                {"time": iso(10, 0, 0), "temperature": -16.0},
                {"time": iso(10, 2, 0), "temperature": -14.0},
            ],
            backup_readings=[
                {"time": iso(10, 0, 0), "temperature": -15.0},
                {"time": iso(10, 2, 0), "temperature": -15.0},
            ],
            allowed_temperature_difference=0.5,
            min_deviation_seconds=0,
        ),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert [d["duration_seconds"] for d in data["deviation_intervals"]] == [30.0, 30.0]
    starts = [d["start"] for d in data["deviation_intervals"]]
    assert starts[0].startswith("2026-09-25T02:00:00")
    assert starts[1].startswith("2026-09-25T02:01:30")
