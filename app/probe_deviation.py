"""冷库主、备双探头温度偏差复核。

与越限复盘（``app.review``）、热暴露核算（``app.exposure``）共用读数校验与
采样断档规则（:func:`app.review.parse_readings` /
:func:`app.review.is_sample_gap`），本模块只增加"两组读数对照"的几何切分：

- 仅在两组读数覆盖时间（各自首末读数之间）的**非空交集**内核算；
- 按双方的采样时刻切分时间轴，得到一组小单元，每个单元内两侧都至多落在
  一条线性采样段上；
- 单元被任一侧的采样断档覆盖时标记为**未知**（不外推、不插值）；
- 两侧均有效的单元上分别线性插值，差值 ``|主 − 备|`` 仍为时间的线性函数，
  求出**严格大于**允许温差的子区间；
- 偏差区间不跨越未知区间合并；连续偏差时长达到最短偏差时长才输出。

所有时间一律用带时区的 ``datetime``（内部统一为 POSIX 秒，秒以 float 表示）。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime

from app.review import (
    _EPS,
    Interval,
    OverSegment,
    Reading,
    ReviewError,
    _is_finite,
    _merge_intervals,
    _merge_over_segments,
    _ts,
    is_sample_gap,
    parse_readings,
)


@dataclass
class ProbeDeviationResult:
    """双探头偏差复核结果（时间以 POSIX 秒表示）。"""

    deviation_intervals: list[Interval] = field(default_factory=list)
    """达到最短偏差时长的连续偏差区间（绝对温差严格大于允许值）。"""

    unknown_intervals: list[Interval] = field(default_factory=list)
    """覆盖时间交集内、任一侧采样断档所覆盖的未知区间。"""

    deviation_seconds: float = 0.0
    """输出的偏差区间总时长（秒）。"""

    unknown_seconds: float = 0.0
    """未知区间总时长（秒）。"""


def _enclosing_index(times: list[float], t: float) -> int:
    """返回包含时刻 ``t`` 的相邻采样段下标：``times[i] <= t <= times[i+1]``。

    ``t`` 恰好等于某个采样点（末点除外）时取以该点为起点的段，保证单元两侧
    端点能用同一条段求值。调用方需保证 ``t`` 位于首末采样时刻之间。
    """
    return min(bisect_right(times, t) - 1, len(times) - 2)


def _value_at(pts: list[Reading], idx: int, t: float) -> float:
    """在线性采样段 ``pts[idx] -> pts[idx+1]`` 上求时刻 ``t`` 的插值温度。"""
    a, b = pts[idx], pts[idx + 1]
    span = b.time - a.time
    if span <= _EPS:
        return a.temp
    return a.temp + (b.temp - a.temp) * (t - a.time) / span


def _deviation_subintervals(
    pa: list[Reading],
    ia: int,
    pb: list[Reading],
    ib: int,
    lo: float,
    hi: float,
    allowed_delta: float,
) -> list[OverSegment]:
    """在两侧均有效的单元 ``[lo, hi)`` 上求绝对温差严格超标的全部子区间。

    两侧温度各自线性，差值 ``d(t) = 主 − 备`` 在单元内也是时间的线性函数，
    故 ``|d(t)|`` 至多在单元两端各有一段超标（中间为 ``d=0`` 的低谷），
    即最多返回两段；恰好等于允许温差的端点不计入（严格大于）。

    每段的 ``over_at_start`` / ``over_at_end`` 表示单元端点处是否严格超标，
    用于决定相邻单元能否在公共断点处合并。
    """
    d_lo = _value_at(pa, ia, lo) - _value_at(pb, ib, lo)
    d_hi = _value_at(pa, ia, hi) - _value_at(pb, ib, hi)

    # 候选边界：两个端点 + |d| 与允许温差相等的内部交叉点。
    # d 线性：分别解 d(t) = +allowed 与 d(t) = -allowed，取落在单元内部的根。
    boundaries: list[float] = [lo, hi]
    if abs(d_hi - d_lo) > _EPS:
        slope = (d_hi - d_lo) / (hi - lo)
        for target in (allowed_delta, -allowed_delta):
            u = (target - d_lo) / slope  # 相对 lo 的偏移（秒）
            if _EPS < u < hi - lo - _EPS:
                boundaries.append(lo + u)
    boundaries.sort()

    # |d| 在相邻边界之间单调（或恒定）：取中点判定，收集中点严格超标的全部相邻对。
    segments: list[OverSegment] = []
    for x, y in zip(boundaries, boundaries[1:]):
        mid = (x + y) * 0.5
        d_mid = _value_at(pa, ia, mid) - _value_at(pb, ib, mid)
        if abs(d_mid) - allowed_delta > 0.0:
            over_at_start = abs(d_lo) - allowed_delta > _EPS and abs(x - lo) < _EPS
            over_at_end = abs(d_hi) - allowed_delta > _EPS and abs(y - hi) < _EPS
            segments.append(OverSegment(x, y, over_at_start, over_at_end))
    return segments


def probe_deviation(
    primary: list[tuple[datetime, float]],
    backup: list[tuple[datetime, float]],
    max_sample_gap_seconds_primary: float,
    max_sample_gap_seconds_backup: float,
    allowed_delta: float,
    min_deviation_seconds: float,
) -> ProbeDeviationResult:
    """复核同一冷库主、备探头的温度偏差。

    参数
    ----
    primary / backup:
        主 / 备探头的 ``(带时区时间, 温度)`` 列表，调用方需保证时间严格递增。
    max_sample_gap_seconds_primary / max_sample_gap_seconds_backup:
        两侧各自允许的最大相邻采样间隔（秒，正数），严格超过即判为采样断档。
    allowed_delta:
        允许温差（°C，非负有限值）；绝对温差**严格大于**该值才算偏差。
    min_deviation_seconds:
        最短偏差时长（秒，非负），连续偏差达到该时长才输出。

    异常
    ----
    ReviewError: 参数非法，或两组读数覆盖时间无非空交集。
    ValueError: 时间缺少时区信息。
    """
    if max_sample_gap_seconds_primary <= 0:
        raise ReviewError("主探头最大采样间隔必须为正数")
    if max_sample_gap_seconds_backup <= 0:
        raise ReviewError("备探头最大采样间隔必须为正数")
    if not _is_finite(allowed_delta) or allowed_delta < 0:
        raise ReviewError("允许温差必须是非负有限数值")
    if not _is_finite(min_deviation_seconds) or min_deviation_seconds < 0:
        raise ReviewError("最短偏差时长必须是非负有限数值")

    pa = parse_readings(primary)
    pb = parse_readings(backup)
    ta = [p.time for p in pa]
    tb = [p.time for p in pb]

    # 仅在两组读数覆盖时间的非空交集内切分；无交集（含仅端点相接）返回 422。
    lo = max(ta[0], tb[0])
    hi = min(ta[-1], tb[-1])
    if hi - lo <= _EPS:
        raise ReviewError("两组读数覆盖时间无非空交集")

    # 按双方采样时刻切分时间轴（去重）；交集端点之外的采样时刻不参与。
    cuts = sorted(
        {t for t in ta if lo <= t <= hi} | {t for t in tb if lo <= t <= hi}
    )

    deviation_segments: list[OverSegment] = []
    unknown_raw: list[Interval] = []

    for x, y in zip(cuts, cuts[1:]):
        if y - x <= _EPS:
            continue
        ia = _enclosing_index(ta, x)
        ib = _enclosing_index(tb, x)
        # 单元内没有任一方的采样点，段下标对整段有效。
        primary_gap = is_sample_gap(
            pa[ia], pa[ia + 1], max_sample_gap_seconds_primary
        )
        backup_gap = is_sample_gap(
            pb[ib], pb[ib + 1], max_sample_gap_seconds_backup
        )
        if primary_gap or backup_gap:
            # 任一侧断档覆盖的交集部分标为未知：不插值、不外推。
            unknown_raw.append(Interval(x, y))
            continue
        segments = _deviation_subintervals(
            pa, ia, pb, ib, x, y, allowed_delta
        )
        deviation_segments.extend(segments)

    # 未知单元先合并（任一侧断档都算未知；相接即连续未知）。
    unknown = _merge_intervals(unknown_raw)

    # 偏差区间在公共断点严格超标时才合并；未知单元天然打断连续性。
    deviation = _merge_over_segments(deviation_segments)

    # 达到最短偏差时长才输出（阈值含等号）。
    deviation = [
        iv for iv in deviation
        if iv.end - iv.start + _EPS >= min_deviation_seconds
    ]

    return ProbeDeviationResult(
        deviation_intervals=deviation,
        unknown_intervals=unknown,
        deviation_seconds=sum(iv.end - iv.start for iv in deviation),
        unknown_seconds=sum(iv.end - iv.start for iv in unknown),
    )
