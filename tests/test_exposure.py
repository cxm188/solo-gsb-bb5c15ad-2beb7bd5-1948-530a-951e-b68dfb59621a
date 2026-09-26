"""热暴露核算测试：精确积分、断档规则、窗口裁剪、预算达标时刻与 422 校验。

温度上限固定 -18°C：-17°C 时 max(温度-上限, 0) = 1，-19°C 时为 0。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.exposure import exposure
from app.main import app
from app.review import ReviewError

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 25, 10, 0, 0, tzinfo=CST)
client = TestClient(app)

PATH = "/api/v1/cold-storage/exposure"


def at(second: float) -> datetime:
    return T0 + timedelta(seconds=second)


def iso(hour: int, minute: int = 0, second: int = 0) -> str:
    return datetime(2026, 9, 25, hour, minute, second, tzinfo=CST).isoformat()


def run(readings, window, budget, limit=-18.0, gap=120.0):
    return exposure(
        readings=[(at(t), temp) for t, temp in readings],
        limit=limit,
        max_sample_gap_seconds=gap,
        window=(at(window[0]), at(window[1])),
        budget=budget,
    )


def base_payload(**overrides):
    payload = {
        "readings": [
            {"time": iso(10, 0, 0), "temperature": -17.0},
            {"time": iso(10, 2, 0), "temperature": -17.0},
        ],
        "temperature_limit": -18.0,
        "max_sample_gap_seconds": 300,
        "storage_window": {"start": iso(10, 0, 0), "end": iso(10, 2, 0)},
        "exposure_budget": 60.0,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 算法层：精确积分与达标时刻
# ---------------------------------------------------------------------------


def test_constant_segment_exact_integral_and_reach():
    # 恒温 -17（超出上限 1℃），窗口 120s：已知量 = 120 ℃·s；预算 60 在 60s 处达到。
    result = run([(0, -17.0), (120, -17.0)], window=(0, 120), budget=60.0)
    assert result.known_exposure == pytest.approx(120.0)
    assert result.unknown_seconds == 0.0
    assert result.budget_reached_at == pytest.approx(T0.timestamp() + 60.0)
    assert result.status == "exceeded"


def test_ramp_integral_is_exact_triangle():
    # -19 -> -17 线性上升，60s 处穿越上限：正部为三角形 0.5*60*1 = 30。
    result = run([(0, -19.0), (120, -17.0)], window=(0, 120), budget=1000.0)
    assert result.known_exposure == pytest.approx(30.0)
    assert result.budget_reached_at is None
    assert result.status == "within"


def test_ramp_reach_solved_inside_segment():
    # 同上斜坡，预算 7.5：u = sqrt(2*7.5*60) = 30s（正部内解析解，非采样点）。
    result = run([(0, -19.0), (120, -17.0)], window=(0, 120), budget=7.5)
    assert result.budget_reached_at == pytest.approx(T0.timestamp() + 90.0)
    assert result.status == "exceeded"


def test_descending_segment_reach():
    # -17 -> -19 下降，30s 处穿越上限：正部 [0,30]，累计 C(u) = u - u²/60。
    # 预算 3.75 -> u = 30 - 15*sqrt(3) ≈ 4.0192（段内求解）。
    result = run([(0, -17.0), (60, -19.0)], window=(0, 60), budget=3.75)
    assert result.known_exposure == pytest.approx(15.0)
    expect = 30.0 - 15.0 * (3.0**0.5)
    assert result.budget_reached_at == pytest.approx(T0.timestamp() + expect)


def test_gap_counts_unknown_and_keeps_cumulative():
    # 断档 [60,300]：未知 240s，不插值、不贡献已知量；累计不清零，跨段累计达标。
    result = run(
        [(0, -17.0), (60, -17.0), (300, -17.0), (360, -17.0)],
        window=(0, 360),
        budget=100.0,
    )
    assert result.known_exposure == pytest.approx(120.0)
    assert result.unknown_seconds == pytest.approx(240.0)
    # 第一段累计 60，断档后第二段再补 40 -> 340s 处达标。
    assert result.budget_reached_at == pytest.approx(T0.timestamp() + 340.0)
    assert result.status == "exceeded"


def test_not_reached_with_unknown_is_indeterminate():
    result = run(
        [(0, -17.0), (60, -17.0), (300, -17.0), (360, -17.0)],
        window=(0, 360),
        budget=1000.0,
    )
    assert result.budget_reached_at is None
    assert result.status == "indeterminate"


def test_window_clipped_to_segment():
    # 窗口 [30, 90] 裁剪恒温段：已知量 = 60，未知 0。
    result = run([(0, -17.0), (120, -17.0)], window=(30, 90), budget=1000.0)
    assert result.known_exposure == pytest.approx(60.0)
    assert result.unknown_seconds == 0.0
    assert result.status == "within"


def test_window_inside_gap_is_all_unknown():
    result = run([(0, -17.0), (300, -17.0)], window=(60, 120), budget=1.0)
    assert result.known_exposure == 0.0
    assert result.unknown_seconds == pytest.approx(60.0)
    assert result.status == "indeterminate"


def test_budget_reached_exactly_at_window_end():
    result = run([(0, -17.0), (60, -17.0)], window=(0, 60), budget=60.0)
    assert result.budget_reached_at == pytest.approx(T0.timestamp() + 60.0)
    assert result.status == "exceeded"


def test_gap_exactly_at_threshold_is_not_unknown():
    # 间隔恰好等于最大允许值：不算断档（与复盘接口规则一致）。
    result = run([(0, -17.0), (120, -17.0)], window=(0, 120), budget=1000.0, gap=120.0)
    assert result.unknown_seconds == 0.0
    assert result.known_exposure == pytest.approx(120.0)


def test_invalid_window_rejected():
    with pytest.raises(ReviewError):
        run([(0, -17.0), (60, -17.0)], window=(30, 30), budget=1.0)  # 空窗口
    with pytest.raises(ReviewError):
        run([(0, -17.0), (60, -17.0)], window=(-10, 30), budget=1.0)  # 早于首读数
    with pytest.raises(ReviewError):
        run([(0, -17.0), (60, -17.0)], window=(30, 120), budget=1.0)  # 晚于末读数


def test_invalid_budget_rejected():
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ReviewError):
            run([(0, -17.0), (60, -17.0)], window=(0, 60), budget=bad)


def test_naive_window_time_rejected():
    with pytest.raises(ValueError):
        exposure(
            readings=[(at(0), -17.0), (at(60), -17.0)],
            limit=-18.0,
            max_sample_gap_seconds=120.0,
            window=(datetime(2026, 9, 25, 10, 0, 0), at(60)),
            budget=1.0,
        )


# ---------------------------------------------------------------------------
# API 层：路由、UTC 输出、422 校验
# ---------------------------------------------------------------------------


def test_api_constant_over_reached():
    resp = client.post(PATH, json=base_payload())
    assert resp.status_code == 200
    data = resp.json()
    assert data["known_exposure"] == 120.0
    assert data["unknown_seconds"] == 0.0
    assert data["status"] == "exceeded"
    assert data["budget_reached_at"].endswith("Z")  # 输出统一 UTC
    assert data["budget_reached_at"].startswith("2026-09-25T02:01:00")


def test_api_ramp_reach_not_at_sample_point():
    payload = base_payload(
        readings=[
            {"time": iso(10, 0, 0), "temperature": -19.0},
            {"time": iso(10, 2, 0), "temperature": -17.0},
        ],
        exposure_budget=7.5,
    )
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["known_exposure"] == 30.0
    # 达标时刻 10:01:30 CST = 02:01:30 UTC：段内解析解，而非下一个采样点 10:02。
    assert data["budget_reached_at"].startswith("2026-09-25T02:01:30")


def test_api_gap_indeterminate():
    payload = base_payload(
        readings=[
            {"time": iso(10, 0, 0), "temperature": -17.0},
            {"time": iso(10, 1, 0), "temperature": -17.0},
            {"time": iso(10, 10, 0), "temperature": -17.0},
            {"time": iso(10, 11, 0), "temperature": -17.0},
        ],
        max_sample_gap_seconds=120,
        storage_window={"start": iso(10, 0, 0), "end": iso(10, 11, 0)},
        exposure_budget=200.0,
    )
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["known_exposure"] == 120.0
    assert data["unknown_seconds"] == 540.0
    assert data["budget_reached_at"] is None
    assert data["status"] == "indeterminate"


def test_api_cumulative_not_reset_by_gap():
    payload = base_payload(
        readings=[
            {"time": iso(10, 0, 0), "temperature": -17.0},
            {"time": iso(10, 1, 0), "temperature": -17.0},
            {"time": iso(10, 10, 0), "temperature": -17.0},
            {"time": iso(10, 11, 0), "temperature": -17.0},
        ],
        max_sample_gap_seconds=120,
        storage_window={"start": iso(10, 0, 0), "end": iso(10, 11, 0)},
        exposure_budget=100.0,
    )
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "exceeded"
    # 第一段累计 60，断档不清零，第二段 10:10:40 CST = 02:10:40 UTC 达标。
    assert data["budget_reached_at"].startswith("2026-09-25T02:10:40")


def test_api_within():
    payload = base_payload(
        readings=[
            {"time": iso(10, 0, 0), "temperature": -19.0},
            {"time": iso(10, 1, 0), "temperature": -19.0},
        ],
        storage_window={"start": iso(10, 0, 0), "end": iso(10, 1, 0)},
        exposure_budget=1.0,
    )
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["known_exposure"] == 0.0
    assert data["unknown_seconds"] == 0.0
    assert data["budget_reached_at"] is None
    assert data["status"] == "within"


def test_api_empty_window_rejected():
    payload = base_payload(
        storage_window={"start": iso(10, 0, 30), "end": iso(10, 0, 30)}
    )
    assert client.post(PATH, json=payload).status_code == 422
    payload = base_payload(
        storage_window={"start": iso(10, 1, 0), "end": iso(10, 0, 30)}
    )
    assert client.post(PATH, json=payload).status_code == 422


def test_api_window_outside_readings_rejected():
    payload = base_payload(
        storage_window={"start": iso(9, 59, 0), "end": iso(10, 1, 0)}
    )
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 422
    assert "首末读数" in resp.text
    payload = base_payload(
        storage_window={"start": iso(10, 0, 0), "end": iso(10, 3, 0)}
    )
    assert client.post(PATH, json=payload).status_code == 422


def test_api_naive_window_rejected():
    payload = base_payload(
        storage_window={"start": "2026-09-25T10:00:00", "end": iso(10, 2, 0)}
    )
    assert client.post(PATH, json=payload).status_code == 422


def test_api_non_positive_budget_rejected():
    assert client.post(PATH, json=base_payload(exposure_budget=0)).status_code == 422
    assert client.post(PATH, json=base_payload(exposure_budget=-5)).status_code == 422


def test_api_reading_validation_reused():
    # 非递增读数
    payload = base_payload(
        readings=[
            {"time": iso(10, 1, 0), "temperature": -17.0},
            {"time": iso(10, 0, 0), "temperature": -17.0},
        ]
    )
    assert client.post(PATH, json=payload).status_code == 422
    # 缺时区读数
    payload = base_payload()
    payload["readings"][0]["time"] = "2026-09-25T10:00:00"
    assert client.post(PATH, json=payload).status_code == 422
    # 非法采样间隔
    assert client.post(PATH, json=base_payload(max_sample_gap_seconds=0)).status_code == 422
    # 单条读数
    payload = base_payload(readings=[{"time": iso(10, 0, 0), "temperature": -17.0}])
    assert client.post(PATH, json=payload).status_code == 422


def test_api_original_review_endpoint_still_works():
    resp = client.post(
        "/api/v1/cold-storage/alarms/review",
        json={
            "readings": [
                {"time": iso(10, 0, 0), "temperature": -17.0},
                {"time": iso(10, 1, 0), "temperature": -17.0},
            ],
            "temperature_limit": -18.0,
            "max_sample_gap_seconds": 120,
            "min_alarm_seconds": 30,
            "maintenance_windows": [],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["alarm_count"] == 1
