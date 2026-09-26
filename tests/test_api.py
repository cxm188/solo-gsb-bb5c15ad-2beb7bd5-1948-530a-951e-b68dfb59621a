"""API 层测试：路由、422 校验、端到端复盘。

温度上限 -18°C：-17°C 越限，-19°C 安全。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app

CST = timezone(timedelta(hours=8))
client = TestClient(app)

PATH = "/api/v1/cold-storage/alarms/review"


def iso(hour: int, minute: int = 0, second: int = 0) -> str:
    return datetime(2026, 9, 25, hour, minute, second, tzinfo=CST).isoformat()


def base_payload(**overrides):
    payload = {
        "readings": [
            {"time": iso(10, 0, 0), "temperature": -17.0},
            {"time": iso(10, 1, 0), "temperature": -17.0},
            {"time": iso(10, 2, 0), "temperature": -17.0},
        ],
        "temperature_limit": -18.0,
        "max_sample_gap_seconds": 120,
        "min_alarm_seconds": 60,
        "maintenance_windows": [],
    }
    payload.update(overrides)
    return payload


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_openapi_available():
    resp = client.get("/openapi.json")
    assert resp.status_code == 200


def test_end_to_end_review():
    # 连续 3 条读数间隔 60s（允许），全程 -17°C 越限，持续 120s。
    resp = client.post(PATH, json=base_payload())
    assert resp.status_code == 200
    data = resp.json()
    assert data["alarm_count"] == 1
    assert data["unknown_count"] == 0
    alarm = data["alarms"][0]
    assert alarm["start"].endswith("Z")  # 输出统一 UTC：10:00 CST -> 02:00 UTC
    assert alarm["start"].startswith("2026-09-25T02:00:00")
    assert alarm["end"].startswith("2026-09-25T02:02:00")
    assert alarm["duration_seconds"] == 120.0
    assert len(data["exceed_intervals"]) == 1


def test_linear_crossing_splits_at_interpolated_point():
    # 10:00 安全(-19) -> 10:02 越限(-17)：插值穿越点在 10:01（即上限 -18）。
    payload = base_payload(
        readings=[
            {"time": iso(10, 0, 0), "temperature": -19.0},
            {"time": iso(10, 2, 0), "temperature": -17.0},
        ],
        max_sample_gap_seconds=300,
        min_alarm_seconds=30,
    )
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["alarm_count"] == 1
    alarm = data["alarms"][0]
    assert alarm["start"].startswith("2026-09-25T02:01:00")
    assert alarm["end"].startswith("2026-09-25T02:02:00")
    assert alarm["duration_seconds"] == 60.0


def test_unknown_gap_returned():
    payload = base_payload(
        readings=[
            {"time": iso(10, 0, 0), "temperature": -17.0},
            {"time": iso(10, 1, 0), "temperature": -17.0},
            {"time": iso(10, 10, 0), "temperature": -17.0},
            {"time": iso(10, 11, 0), "temperature": -17.0},
        ]
    )
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["unknown_count"] == 1
    unk = data["unknown_intervals"][0]
    assert unk["duration_seconds"] == 540.0
    assert data["alarm_count"] == 2  # 断档两侧各一个报警，没有被跨越合并


def test_maintenance_window_splits_alarm():
    payload = base_payload(
        readings=[
            {"time": iso(10, 0, 0), "temperature": -17.0},
            {"time": iso(10, 4, 0), "temperature": -17.0},
        ],
        maintenance_windows=[
            {"start": iso(10, 1, 0), "end": iso(10, 3, 0)}
        ],
        max_sample_gap_seconds=300,
        min_alarm_seconds=60,
    )
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert [a["duration_seconds"] for a in data["alarms"]] == [60.0, 60.0]


def test_short_residual_is_filtered_out():
    payload = base_payload(
        readings=[
            {"time": iso(10, 0, 0), "temperature": -17.0},
            {"time": iso(10, 1, 0), "temperature": -17.0},
        ],
        maintenance_windows=[
            {"start": iso(10, 0, 20), "end": iso(10, 1, 0)}
        ],
        min_alarm_seconds=30,
    )
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["alarms"] == []  # 豁免后只剩 20s，不足 30s
    assert len(data["exceed_intervals"]) == 1


def test_non_increasing_readings_rejected():
    payload = base_payload(
        readings=[
            {"time": iso(10, 1, 0), "temperature": -17.0},
            {"time": iso(10, 0, 0), "temperature": -17.0},
        ]
    )
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 422
    assert "严格递增" in resp.text


def test_equal_timestamps_rejected():
    payload = base_payload(
        readings=[
            {"time": iso(10, 0, 0), "temperature": -17.0},
            {"time": iso(10, 0, 0), "temperature": -17.5},
        ]
    )
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 422


def test_invalid_exemption_window_rejected():
    payload = base_payload(
        maintenance_windows=[
            {"start": iso(10, 5, 0), "end": iso(10, 4, 0)}
        ]
    )
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 422


def test_naive_reading_time_rejected():
    payload = base_payload()
    payload["readings"][0]["time"] = "2026-09-25T10:00:00"
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 422


def test_bad_gap_parameter_rejected():
    resp = client.post(PATH, json=base_payload(max_sample_gap_seconds=0))
    assert resp.status_code == 422
    resp = client.post(PATH, json=base_payload(min_alarm_seconds=-1))
    assert resp.status_code == 422


def test_single_reading_rejected():
    payload = base_payload(
        readings=[{"time": iso(10, 0, 0), "temperature": -17.0}]
    )
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 422
