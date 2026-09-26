"""冷库温度越限复盘核心算法。

处理流程：
1. 校验参数（上限、最大采样间隔、最短报警时长必须合理；豁免时段必须有效）。
2. 相邻且时间差不超过 ``max_sample_gap_seconds`` 的读数之间线性插值；
   时间差超过该阈值的视为"采样断档"——断档区间状态未知，越限区间不跨越断档。
3. 合并得到超过温度上限的连续区间（恰好等于上限不算越限）。
4. 从越限区间中扣除维护豁免时段（重叠部分不参与报警计时）。
5. 扣减后的各段时长 >= ``min_alarm_seconds`` 才作为报警输出。

所有时间一律用带时区的 ``datetime``（内部统一为 UTC，秒以 float 表示）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

_EPS = 1e-9


def _ts(dt: datetime) -> float:
    """把带时区的 datetime 转成 POSIX 秒（float）。"""
    if dt.tzinfo is None:
        raise ValueError("时间必须带时区信息")
    return dt.timestamp()


def _dt(seconds: float) -> datetime:
    """POSIX 秒转回带 UTC 时区的 datetime。"""
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


class ReviewError(ValueError):
    """请求语义不合法（参数或时段无效）。"""


@dataclass(frozen=True)
class Interval:
    """半开时间区间 [start, end)，单位：POSIX 秒。"""

    start: float
    end: float


@dataclass(frozen=True)
class Reading:
    time: float
    temp: float


@dataclass
class OverSegment:
    """一段已知数据上的越限区间。

    over_at_start / over_at_end 标记区间端点处的温度是否"严格超过"上限，
    用于决定相邻两段能否在公共端点处连续（端点温度等于上限时不能合并）。
    """

    start: float
    end: float
    over_at_start: bool
    over_at_end: bool


@dataclass
class ReviewResult:
    exceed_intervals: list[Interval] = field(default_factory=list)
    """扣除豁免前的原始越限连续区间（含时间戳，已转 UTC）。"""

    alarm_intervals: list[Interval] = field(default_factory=list)
    """扣减豁免、并通过最短时长筛选后的报警区间（含时间戳，已转 UTC）。"""

    unknown_intervals: list[Interval] = field(default_factory=list)
    """采样断档区间（含时间戳，已转 UTC）。"""


# ---------------------------------------------------------------------------
# 公共校验与断档判定（越限复盘与热暴露核算共用）
# ---------------------------------------------------------------------------


def prepare_readings(
    readings: list[tuple[datetime, float]],
    limit: float,
    max_sample_gap_seconds: float,
) -> list[Reading]:
    """校验公共参数并解析读数。

    - 温度上限必须是有限数值；
    - 最大采样间隔必须为正数；
    - 至少需要 2 条读数，且温度均为有限数值（拒绝 NaN/Infinity）。

    调用方需保证读数时间带时区且严格递增（API 层由 schema 校验）。
    """
    if not _is_finite(limit):
        raise ReviewError("温度上限必须是有限数值")
    if max_sample_gap_seconds <= 0:
        raise ReviewError("最大采样间隔必须为正数")
    if len(readings) < 2:
        raise ReviewError("至少需要 2 条读数")
    pts = [Reading(_ts(t), float(temp)) for t, temp in readings]
    for p in pts:
        if not _is_finite(p.temp):
            raise ReviewError("温度读数必须是有限数值（不接受 NaN/Infinity）")
    return pts


def is_sample_gap(a: Reading, b: Reading, max_sample_gap_seconds: float) -> bool:
    """相邻读数是否构成采样断档：间隔严格超过阈值（恰好等于不算）。"""
    return b.time - a.time - max_sample_gap_seconds > _EPS


# ---------------------------------------------------------------------------
# 区间工具
# ---------------------------------------------------------------------------


def _subtract_intervals(
    base: list[Interval], exemptions: list[Interval]
) -> list[Interval]:
    """从一批互不重叠、有序的基础区间中扣减豁免区间，返回剩余的半开区间。

    相接（touch）的两段不会被合并：若豁免恰好把某区间切成两段，结果按两段返回。
    """
    result: list[Interval] = []
    for iv in base:
        pieces = [iv]
        for ex in exemptions:
            next_pieces: list[Interval] = []
            for piece in pieces:
                # 无交集（仅端点相接不算重叠）。
                if ex.end <= piece.start + _EPS or ex.start >= piece.end - _EPS:
                    next_pieces.append(piece)
                    continue
                left = Interval(piece.start, min(piece.end, ex.start))
                right = Interval(max(piece.start, ex.end), piece.end)
                if left.end - left.start > _EPS:
                    next_pieces.append(left)
                if right.end - right.start > _EPS:
                    next_pieces.append(right)
            pieces = next_pieces
        result.extend(pieces)
    return result


def _merge_exemptions(raw: list[Interval]) -> list[Interval]:
    """合并重叠/相接的豁免时段。"""
    if not raw:
        return []
    ordered = sorted(raw, key=lambda iv: iv.start)
    merged = [ordered[0]]
    for iv in ordered[1:]:
        last = merged[-1]
        if iv.start <= last.end + _EPS:
            merged[-1] = Interval(last.start, max(last.end, iv.end))
        else:
            merged.append(iv)
    return merged


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------


def _over_subinterval(
    a: Reading, b: Reading, limit: float
) -> OverSegment | None:
    """求相邻读数 a->b 的线性段上温度严格超过 limit 的子区间。

    线性插值在每段内部温度单调（或恒定），所以越限部分至多是一个连续子区间。
    恰好等于上限的端点不计入越限。
    """
    span = b.time - a.time
    if span <= _EPS:
        return None

    # 候选边界：两个端点 + 温度与上限相等的内部交叉点（至多一个）。
    # 端点本身可能恰好等于上限（不越限），但仍是越限子区间的几何边界。
    boundaries: list[float] = [a.time, b.time]
    if b.temp != a.temp:
        t_cross = (limit - a.temp) / (b.temp - a.temp)
        if 0.0 < t_cross < 1.0:
            boundaries.append(a.time + t_cross * span)
    boundaries.sort()

    # 线性段上温度单调（或恒定）：逐对相邻边界取中点判定符号，
    # 中点严格高于上限的那一对即越限子区间（最多一对）。
    for lo, hi in zip(boundaries, boundaries[1:]):
        t_mid = ((lo + hi) / 2 - a.time) / span
        mid_temp = a.temp + (b.temp - a.temp) * t_mid
        if mid_temp > limit:
            over_at_start = a.temp > limit and abs(lo - a.time) < _EPS
            over_at_end = b.temp > limit and abs(hi - b.time) < _EPS
            return OverSegment(lo, hi, over_at_start, over_at_end)
    return None


def _merge_over_segments(segs: list[OverSegment]) -> list[Interval]:
    """把逐段求出的越限子区间合并为连续区间。

    仅当两段在公共采样点相接、且该点温度严格超过上限时才合并；
    公共点温度恰好等于上限（两侧分别刚好转下、刚好转上）时保持断开。
    """
    merged: list[Interval] = []
    for seg in segs:
        if seg.end - seg.start <= _EPS:
            continue
        if merged and abs(seg.start - merged[-1].end) < _EPS and seg.over_at_start:
            merged[-1] = Interval(merged[-1].start, seg.end)
        else:
            merged.append(Interval(seg.start, seg.end))
    return merged


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def review(
    readings: list[tuple[datetime, float]],
    limit: float,
    max_sample_gap_seconds: float,
    min_alarm_seconds: float,
    exemptions: list[tuple[datetime, datetime]] | None = None,
) -> ReviewResult:
    """执行一次温度越限复盘。

    参数
    ----
    readings:
        ``(带时区时间, 温度)`` 列表，调用方需保证时间严格递增。
    limit:
        温度上限，温度严格大于该值视为越限。
    max_sample_gap_seconds:
        允许的最大相邻采样间隔（秒，正数）。超过则判定为采样断档。
    min_alarm_seconds:
        最短报警时长（秒，非负）。扣减豁免后的连续越限达到该时长才报警。
    exemptions:
        维护豁免时段 ``[(开始, 结束), ...]``，区间内的越限不计入报警。

    异常
    ----
    ReviewError: 参数非法或时段无效。
    ValueError: 时间缺少时区信息。
    """
    if min_alarm_seconds < 0:
        raise ReviewError("最短报警时长不能为负数")
    pts = prepare_readings(readings, limit, max_sample_gap_seconds)

    raw_ex: list[Interval] = []
    for start, end in exemptions or []:
        s, e = _ts(start), _ts(end)
        if e <= s + _EPS:
            raise ReviewError("豁免时段的结束时间必须晚于开始时间")
        raw_ex.append(Interval(s, e))
    ex_merged = _merge_exemptions(raw_ex)

    unknown: list[Interval] = []
    over_segments: list[OverSegment] = []

    for a, b in zip(pts, pts[1:]):
        if is_sample_gap(a, b, max_sample_gap_seconds):
            # 采样断档：状态未知，不插值、不跨越。
            unknown.append(Interval(a.time, b.time))
            continue
        seg = _over_subinterval(a, b, limit)
        if seg is not None:
            over_segments.append(seg)

    exceed = _merge_over_segments(over_segments)
    remainders = _subtract_intervals(exceed, ex_merged)
    alarms = [
        iv for iv in remainders if iv.end - iv.start + _EPS >= min_alarm_seconds
    ]

    return ReviewResult(
        exceed_intervals=exceed,
        alarm_intervals=alarms,
        unknown_intervals=unknown,
    )


def _is_finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


# 供 API 层做时间戳转换。
def to_utc(seconds: float) -> datetime:
    return _dt(seconds)
