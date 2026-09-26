"""冷库温度越限复盘与热暴露核算 API。

启动（容器内 / 本地 venv 均由入口脚本完成依赖安装与初始化）：

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import exposure as exposure_logic
from app import review as review_logic
from app.schemas import (
    AlarmReviewRequest,
    AlarmReviewResponse,
    ExposureRequest,
    ExposureResponse,
    IntervalOut,
)

app = FastAPI(
    title="冷库温度越限复盘 API",
    version="1.1.0",
    description=(
        "接收同一冷库的带时区温度读数及阈值配置，在允许的相邻读数间线性插值，"
        "计算超过温度上限的连续区间；采样断档标记为未知且不跨越；"
        "扣除维护豁免时段后按最短报警时长筛选报警。"
        "另提供货物存放窗口热暴露核算：窗口内精确积分 max(温度−上限, 0)，"
        "跨段累计并求解已知累计量首次达到预算的时刻。"
    ),
)


def _to_out(iv: review_logic.Interval) -> IntervalOut:
    duration = round(iv.end - iv.start, 6)
    return IntervalOut(
        start=review_logic.to_utc(iv.start),
        end=review_logic.to_utc(iv.end),
        duration_seconds=duration,
    )


@app.get("/health", tags=["运维"])
def health() -> dict[str, str]:
    """健康检查。"""
    return {"status": "ok"}


@app.post(
    "/api/v1/cold-storage/alarms/review",
    response_model=AlarmReviewResponse,
    tags=["温度越限复盘"],
    responses={400: {"description": "无效时段或不支持的请求内容"}},
)
def review_alarms(payload: AlarmReviewRequest) -> AlarmReviewResponse:
    """执行一次温度越限复盘。

    - 读数时间必须带时区且严格递增，否则返回 422；
    - 维护豁免时段必须带时区且结束严格晚于开始，否则返回 422；
    - 采样间隔超过 ``max_sample_gap_seconds`` 的相邻读数之间标记为未知区间，
      越限区间不会跨越断档；
    - 温度恰好等于上限不算越限；豁免时段与越限区间的重叠部分不计入报警时长。
    """
    readings = [(r.time, r.temperature) for r in payload.readings]
    exemptions = [
        (w.start, w.end) for w in payload.maintenance_windows
    ]

    try:
        result = review_logic.review(
            readings=readings,
            limit=payload.temperature_limit,
            max_sample_gap_seconds=payload.max_sample_gap_seconds,
            min_alarm_seconds=payload.min_alarm_seconds,
            exemptions=exemptions,
        )
    except review_logic.ReviewError as exc:
        # schema 层未覆盖到的语义错误统一返回 400。
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    alarms = [_to_out(iv) for iv in result.alarm_intervals]
    exceeds = [_to_out(iv) for iv in result.exceed_intervals]
    unknowns = [_to_out(iv) for iv in result.unknown_intervals]

    return AlarmReviewResponse(
        alarms=alarms,
        exceed_intervals=exceeds,
        unknown_intervals=unknowns,
        alarm_count=len(alarms),
        unknown_count=len(unknowns),
    )


@app.post(
    "/api/v1/cold-storage/exposure",
    response_model=ExposureResponse,
    tags=["热暴露核算"],
    responses={400: {"description": "无效时段或不支持的请求内容"}},
)
def compute_exposure(payload: ExposureRequest) -> ExposureResponse:
    """核算货物存放窗口内的热暴露。

    - 读数校验与采样断档规则与复盘接口一致（时间带时区且严格递增，否则 422）；
    - 存放窗口必须带时区、非空且位于首末读数之间，否则返回 422；
    - 仅在有效采样段内线性插值，裁剪到窗口后精确积分 ``max(温度−上限, 0)``（℃·秒），
      跨段累计，不扣维护豁免；
    - 断档区间不插值、不贡献已知量（计入窗口内未知秒数），但已累计的已知量不清零；
    - 已知累计量首次达到预算的时刻在段内解析求解（而非取下一个采样点），未达到为 null；
    - 状态：已知量达到预算为 ``exceeded``；否则窗口内有未知时长为 ``indeterminate``，
      无未知时长为 ``within``。
    """
    readings = [(r.time, r.temperature) for r in payload.readings]

    try:
        result = exposure_logic.exposure(
            readings=readings,
            limit=payload.temperature_limit,
            max_sample_gap_seconds=payload.max_sample_gap_seconds,
            window=(payload.storage_window.start, payload.storage_window.end),
            budget=payload.exposure_budget,
        )
    except review_logic.ReviewError as exc:
        # schema 层未覆盖到的语义错误统一返回 400。
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    return ExposureResponse(
        known_exposure=round(result.known_exposure, 6),
        unknown_seconds=round(result.unknown_seconds, 6),
        budget_reached_at=(
            review_logic.to_utc(result.budget_reached_at)
            if result.budget_reached_at is not None
            else None
        ),
        status=result.status,
    )


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("APP_HOST", "0.0.0.0")
    port = int(os.environ.get("APP_PORT", "8000"))
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
