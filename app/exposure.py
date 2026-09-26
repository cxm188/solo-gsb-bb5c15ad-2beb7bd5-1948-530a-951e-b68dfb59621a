"""冷库货物存放窗口热暴露核算。

与越限复盘（``app.review``）共用读数校验与采样断档规则：

- 仅在有效采样段（相邻读数间隔不超过 ``max_sample_gap_seconds``）内线性插值；
- 断档区间不插值、不贡献已知热暴露量（计为窗口内未知秒数），但已累计的已知量不清零；
- 在货物存放窗口 ``[start, end)`` 内精确积分 ``max(温度 - 上限, 0)``（单位 ℃·秒），
  跨段累计，不扣维护豁免；
- 已知累计量首次达到预算的时刻在段内解析求解（二次方程求根），而非取下一个采样点。

状态判定：已知量达到预算 → ``exceeded``；未达到但窗口内存在未知时长 →
``indeterminate``；未达到且无未知时长 → ``within``。

所有时间一律用带时区的 ``datetime``（内部统一为 POSIX 秒，秒以 float 表示）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from app.review import (
    _EPS,
    Reading,
    ReviewError,
    _is_finite,
    _ts,
    is_sample_gap,
    prepare_readings,
)

# 判定“达到预算”的相对容差（1e-9），仅用于吸收浮点累计误差。
_REACH_TOL = 1e-9

STATUS_EXCEEDED = "exceeded"
STATUS_INDETERMINATE = "indeterminate"
STATUS_WITHIN = "within"


@dataclass(frozen=True)
class ExposureResult:
    """热暴露核算结果（时间以 POSIX 秒表示）。"""

    known_exposure: float
    """窗口内已知热暴露量（℃·秒），仅统计有效采样段，不扣维护豁免。"""

    unknown_seconds: float
    """窗口内采样断档（状态未知）的总秒数。"""

    budget_reached_at: float | None
    """已知累计量首次达到预算的时刻（POSIX 秒）；未达到为 None。"""

    status: str
    """exceeded / indeterminate / within。"""


def _segment_exposure(
    a: Reading,
    b: Reading,
    limit: float,
    lo: float,
    hi: float,
    remaining: float,
) -> tuple[float, float | None]:
    """在有效采样段 a->b 的 [lo, hi] 子区间上积分 max(温度-上限, 0)。

    温度在段内线性变化，``max(·, 0)`` 的正部至多一段；正部上被积函数仍为线性，
    梯形公式即精确积分。若段内累计可以达到 ``remaining``（剩余预算），则在正部上
    解二次方程求首次达标时刻——**在段内求解，不取采样点**。

    返回 ``(该段贡献的热暴露量, 段内首次达标时刻或 None)``。
    """
    span = b.time - a.time
    rate = (b.temp - a.temp) / span  # 温度变化率（℃/秒）
    f_lo = a.temp + rate * (lo - a.time) - limit
    f_hi = a.temp + rate * (hi - a.time) - limit

    if f_lo <= 0.0 and f_hi <= 0.0:
        return 0.0, None

    if f_lo >= 0.0 and f_hi >= 0.0:
        p_lo, p_hi, fp_lo, fp_hi = lo, hi, f_lo, f_hi
    else:
        # 线性段上至多一个过零点，正部取过零点的一侧。
        cross = lo + (0.0 - f_lo) * (hi - lo) / (f_hi - f_lo)
        if f_lo > 0.0:
            p_lo, p_hi, fp_lo, fp_hi = lo, cross, f_lo, 0.0  # 下降穿越
        else:
            p_lo, p_hi, fp_lo, fp_hi = cross, hi, 0.0, f_hi  # 上升穿越

    width = p_hi - p_lo
    if width <= 0.0:
        return 0.0, None
    # 正部上被积函数线性：梯形面积即精确积分。
    total = (fp_lo + fp_hi) * 0.5 * width

    reach: float | None = None
    if remaining <= total + _REACH_TOL * max(1.0, abs(total)):
        # 正部上 f(p_lo + u) = fp_lo + slope·u，
        # 累计 C(u) = fp_lo·u + slope·u²/2；令 C(u) = remaining 求最小非负根。
        slope = (fp_hi - fp_lo) / width
        target = max(remaining, 0.0)
        if abs(slope) < _EPS:
            u = target / fp_lo if fp_lo > 0.0 else 0.0
        else:
            disc = fp_lo * fp_lo + 2.0 * slope * target
            u = (math.sqrt(max(disc, 0.0)) - fp_lo) / slope
        u = min(max(u, 0.0), width)  # 浮点误差防护：解夹在正部内
        reach = p_lo + u
    return total, reach


def exposure(
    readings: list[tuple[datetime, float]],
    limit: float,
    max_sample_gap_seconds: float,
    window: tuple[datetime, datetime],
    budget: float,
) -> ExposureResult:
    """核算货物存放窗口内的热暴露。

    参数
    ----
    readings / limit / max_sample_gap_seconds:
        与 :func:`app.review.review` 相同（读数校验与断档规则复用同一套实现）。
    window:
        货物存放窗口 ``(start, end)``，半开区间，须非空且位于首末读数时间之间。
    budget:
        正数热暴露预算（℃·秒）。

    异常
    ----
    ReviewError: 参数非法或存放窗口无效。
    ValueError: 时间缺少时区信息。
    """
    if not _is_finite(budget) or budget <= 0:
        raise ReviewError("热暴露预算必须是正的有限数值")
    pts = prepare_readings(readings, limit, max_sample_gap_seconds)

    ws, we = _ts(window[0]), _ts(window[1])
    if we <= ws + _EPS:
        raise ReviewError("货物存放窗口必须非空（结束时间严格晚于开始时间）")
    if ws < pts[0].time - _EPS or we > pts[-1].time + _EPS:
        raise ReviewError("货物存放窗口必须位于首末读数时间之间")

    known = 0.0
    unknown = 0.0
    reach: float | None = None

    # 相邻读数段 [a, b] 两两半开且首尾相接，整体恰好覆盖 [首读数, 末读数]。
    for a, b in zip(pts, pts[1:]):
        # 裁剪到存放窗口。
        lo = max(a.time, ws)
        hi = min(b.time, we)
        if hi <= lo + _EPS:
            continue
        if is_sample_gap(a, b, max_sample_gap_seconds):
            # 断档：不插值、不贡献已知量；窗口内部分计为未知秒数；累计不清零。
            unknown += hi - lo
            continue
        seg_total, seg_reach = _segment_exposure(
            a, b, limit, lo, hi, budget - known
        )
        known += seg_total
        if reach is None and seg_reach is not None:
            reach = seg_reach

    if reach is not None:
        status = STATUS_EXCEEDED
    elif unknown > _EPS:
        status = STATUS_INDETERMINATE
    else:
        status = STATUS_WITHIN

    return ExposureResult(
        known_exposure=known,
        unknown_seconds=unknown,
        budget_reached_at=reach,
        status=status,
    )
