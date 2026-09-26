"""请求与响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class TemperatureReading(BaseModel):
    """一条带时区的温度读数。"""

    time: datetime = Field(..., description="读数时间，必须带时区（如 2026-09-25T10:00:00+08:00）")
    temperature: float = Field(..., description="冷库温度（°C）")


class TimePeriod(BaseModel):
    """一个半开时间区间 [start, end)，维护豁免时段与货物存放窗口用。"""

    start: datetime = Field(..., description="开始时间（含），必须带时区")
    end: datetime = Field(..., description="结束时间（不含），必须带时区，且严格晚于开始时间")

    @model_validator(mode="after")
    def _check_order(self) -> "TimePeriod":
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start 与 end 都必须带时区")
        if self.end <= self.start:
            raise ValueError("end 必须严格晚于 start")
        return self


class ReadingsRequestBase(BaseModel):
    """读数、温度上限与最大采样间隔的公共字段与校验（复盘 / 热暴露共用）。"""

    readings: list[TemperatureReading] = Field(
        ..., min_length=2, description="同一冷库的温度读数，时间必须严格递增"
    )
    temperature_limit: float = Field(..., description="温度上限（°C），严格大于才算越限")
    max_sample_gap_seconds: float = Field(
        ..., gt=0, description="允许的最大相邻采样间隔（秒），超过即视为采样断档"
    )

    @field_validator("readings")
    @classmethod
    def _check_strictly_increasing(cls, value: list[TemperatureReading]):
        if not value:
            return value
        for item in value:
            if item.time.tzinfo is None:
                raise ValueError("每条读数的 time 都必须带时区")
        prev = value[0].time
        for item in value[1:]:
            if item.time <= prev:
                raise ValueError("读数时间必须严格递增")
            prev = item.time
        return value

    @field_validator("temperature_limit")
    @classmethod
    def _check_limit_finite(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("温度上限必须是有限数值")
        return value


class AlarmReviewRequest(ReadingsRequestBase):
    """复盘请求。"""

    min_alarm_seconds: float = Field(
        ..., ge=0, description="最短报警时长（秒），扣减豁免后达到该时长才报警"
    )
    maintenance_windows: list[TimePeriod] = Field(
        default_factory=list, description="维护豁免时段，落在其中的越限时间不计入报警"
    )


class ExposureRequest(ReadingsRequestBase):
    """热暴露核算请求。"""

    storage_window: TimePeriod = Field(
        ...,
        description=(
            "带时区的货物存放窗口 [start, end)，必须非空，且整体位于首末读数时间之间"
        ),
    )
    exposure_budget: float = Field(
        ..., gt=0, description="正数热暴露预算（℃·秒）"
    )

    @model_validator(mode="after")
    def _check_window_within_readings(self) -> "ExposureRequest":
        # 复用读数校验后，readings 必然非空且严格递增。
        first = self.readings[0].time
        last = self.readings[-1].time
        if self.storage_window.start < first or self.storage_window.end > last:
            raise ValueError("货物存放窗口必须位于首末读数时间之间")
        return self


class IntervalOut(BaseModel):
    start: datetime = Field(..., description="区间开始时间（含），UTC")
    end: datetime = Field(..., description="区间结束时间（不含），UTC")
    duration_seconds: float = Field(..., description="持续时长（秒）")


class AlarmReviewResponse(BaseModel):
    """复盘响应。"""

    alarms: list[IntervalOut] = Field(
        default_factory=list,
        description="报警区间：扣减维护豁免后、时长达到最短报警时长的连续越限区间",
    )
    exceed_intervals: list[IntervalOut] = Field(
        default_factory=list, description="扣减豁免前的全部连续越限区间（仅作复盘参考）"
    )
    unknown_intervals: list[IntervalOut] = Field(
        default_factory=list, description="采样断档区间，状态未知，越限区间不会跨越这些区间"
    )
    alarm_count: int
    unknown_count: int


class ExposureResponse(BaseModel):
    """热暴露核算响应。"""

    known_exposure: float = Field(
        ...,
        description="窗口内已知热暴露量（℃·秒）：仅统计有效采样段，不扣维护豁免",
    )
    unknown_seconds: float = Field(
        ..., description="窗口内采样断档（状态未知、不插值）的总秒数"
    )
    budget_reached_at: datetime | None = Field(
        default=None,
        description=(
            "已知累计量首次达到热暴露预算的时刻（UTC，在段内解析求解）；"
            "未达到则为 null"
        ),
    )
    status: Literal["exceeded", "indeterminate", "within"] = Field(
        ...,
        description=(
            "exceeded=已知量已达到预算；indeterminate=未达到但窗口内存在未知时长；"
            "within=未达到且窗口内无未知时长"
        ),
    )
