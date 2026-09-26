"""同一冷库主、备双探头温度偏差复核。

与越限复盘（``app.review``）、热暴露核算（``app.exposure``）复用同一套读数校验
（:func:`app.review.prepare_readings`）与采样断档判定
（:func:`app.review.is_sample_gap`）。

处理流程：
1. 分别校验两组读数（带时区、严格递增、温度有限），各自按自己的
   ``max_sample_gap_seconds`` 判定采样断档；
2. 取两组读数覆盖时间 ``[首读数, 末读数]`` 的交集，交集为空则报错（422）；
3. 仅在该交集内，把两侧全部采样时刻作为切点切分时间轴；
4. 对每个小段，在其中点定位两侧各自所属的相邻读数对：任一侧的读数对构成断档，
   该段即**未知**（断档在交集内的部分）；两侧都有效时，用各自有效采样段上的
   **线性插值**得到两侧温度，计算主−备温差；
5. 温差 ``abs(T主 − T备)`` **严格大于**允许温差的连续区间合并输出，
   持续时长达到最短偏差时长才保留；未知段**不外推**，偏差区间**不跨越**未知段。

所有时间内部统一为 POSIX 秒（float），API 输出时统一转 UTC。
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
    is_sample_gap,
    prepare_readings,
)


@dataclass(frozen=True)
class _Side:
    """一侧探头（主或备）的解析结果。"""

    pts: list[Reading]
    times: tuple[float, ...]

    @property
    def coverage(self) -> Interval:
        return Interval(self.pts[0].time, self.pts[-1].time)

    def pair_index_at(self, t: float) -> int | None:
        """t 所属的相邻读数对下标 i（段 ``[pts[i], pts[i+1]]``）。

        覆盖区间内部按中点定位；任一端点贴近首/末读数时直接取首/末段。
        落到覆盖区间外返回 None（交集裁剪后正常不会发生）。
        """
        if t < self.pts[0].time - _EPS or t > self.pts[-1].time + _EPS:
            return None
        if t <= self.pts[0].time + _EPS:
            return 0
        if t >= self.pts[-1].time - _EPS:
            return len(self.pts) - 2
        # bisect_right 得到 t 之后第一个采样点下标，减 1 即 t 所在段；
        # t 恰好落在采样点上时取该点**之后**的段（与段内中点归属一致）。
        i = bisect_right(self.times, t) - 1
        return min(max(i, 0), len(self.pts) - 2)


@dataclass
class DeviationResult:
    """双探头偏差复核结果（时间以 POSIX 秒表示）。"""

    deviation_intervals: list[Interval] = field(default_factory=list)
    """绝对温差严格大于允许温差、且时长达到最短偏差时长的连续区间。"""

    unknown_intervals: list[Interval] = field(default_factory=list)
    """交集中任一侧采样断档覆盖的部分（状态未知，不插值、不跨越）。"""

    deviation_seconds: float = 0.0
    """偏差区间总时长（秒）。"""

    unknown_seconds: float = 0.0
    """未知区间总时长（秒）。"""


def _build_side(
    readings: list[tuple[datetime, float]],
    max_sample_gap_seconds: float,
) -> _Side:
    """校验并解析一侧探头读数（断档在扫描时按各段实时判定）。"""
    pts = prepare_readings(
        readings,
        max_sample_gap_seconds=max_sample_gap_seconds,
    )
    return _Side(
        pts=pts,
        times=tuple(p.time for p in pts),
    )


def _deviation_pieces(
    lo: float,
    hi: float,
    primary: _Side,
    backup: _Side,
    ip: int,
    ib: int,
    tolerance: float,
) -> list[OverSegment]:
    """在两侧都有效的小段 [lo, hi] 上求绝对温差严格超阈的子区间。

    两侧温度分别在各自线性段上插值，温差在本小段内是时间的**线性**函数
    ``d(t) = m·t + c``，``|d(t)|`` 超阈部分至多两段。候选边界取小段端点与
    ``d(t) = ±tolerance`` 的解析根，逐对取中点判定严格超阈。
    """
    ap, bp = primary.pts[ip], primary.pts[ip + 1]
    ab, bb = backup.pts[ib], backup.pts[ib + 1]

    span_p = bp.time - ap.time
    span_b = bb.time - ab.time
    mp = (bp.temp - ap.temp) / span_p
    mb = (bb.temp - ab.temp) / span_b

    def diff_at(t: float) -> float:
        tp = ap.temp + mp * (t - ap.time)
        tb = ab.temp + mb * (t - ab.time)
        return tp - tb

    d_lo = diff_at(lo)
    d_hi = diff_at(hi)
    slope = (d_hi - d_lo) / (hi - lo)

    boundaries = {lo, hi}
    if abs(slope) > _EPS:
        # d(t) = +tolerance 与 d(t) = -tolerance 在段内的根。
        for target in (tolerance, -tolerance):
            root = lo + (target - d_lo) / slope
            if lo + _EPS < root < hi - _EPS:
                boundaries.add(root)
    ordered = sorted(boundaries)

    pieces: list[OverSegment] = []
    for seg_lo, seg_hi in zip(ordered[:-1], ordered[1:]):
        if seg_hi - seg_lo <= _EPS:
            continue
        if abs(diff_at((seg_lo + seg_hi) / 2.0)) - tolerance > 0.0:
            over_at_start = abs(diff_at(seg_lo)) - tolerance > _EPS
            over_at_end = abs(diff_at(seg_hi)) - tolerance > _EPS
            pieces.append(OverSegment(seg_lo, seg_hi, over_at_start, over_at_end))
    return pieces


def deviation_review(
    primary_readings: list[tuple[datetime, float]],
    backup_readings: list[tuple[datetime, float]],
    primary_max_sample_gap_seconds: float,
    backup_max_sample_gap_seconds: float,
    allowed_temperature_difference: float,
    min_deviation_seconds: float,
) -> DeviationResult:
    """执行一次主、备双探头温度偏差复核。

    参数
    ----
    primary_readings / backup_readings:
        主 / 备探头的 ``(带时区时间, 温度)`` 列表，各自至少 2 条、时间严格递增。
    primary_max_sample_gap_seconds / backup_max_sample_gap_seconds:
        主 / 备探头各自允许的最大相邻采样间隔（秒，正数），严格超过即断档。
    allowed_temperature_difference:
        允许温差（°C，非负有限值）；``abs(T主 − T备)`` 严格大于才算偏差，
        恰好等于不算。
    min_deviation_seconds:
        最短偏差时长（秒，非负），连续偏差区间达到该时长才输出（含等号）。

    异常
    ----
    ReviewError: 参数非法，或两组读数覆盖时间没有非空交集。
    ValueError: 时间缺少时区信息。
    """
    if not _is_finite(allowed_temperature_difference):
        raise ReviewError("允许温差必须是有限数值")
    if allowed_temperature_difference < 0:
        raise ReviewError("允许温差不能为负数")
    if not _is_finite(min_deviation_seconds) or min_deviation_seconds < 0:
        raise ReviewError("最短偏差时长必须是非负有限数值")

    primary = _build_side(primary_readings, primary_max_sample_gap_seconds)
    backup = _build_side(backup_readings, backup_max_sample_gap_seconds)

    cp, cb = primary.coverage, backup.coverage
    lo = max(cp.start, cb.start)
    hi = min(cp.end, cb.end)
    if hi - lo <= _EPS:
        raise ReviewError("主、备探头读数的覆盖时间没有非空交集")

    # 交集内按两侧采样时刻切分时间轴（只保留落在交集内部的切点）。
    cuts = {lo, hi}
    for t in primary.times:
        if lo + _EPS < t < hi - _EPS:
            cuts.add(t)
    for t in backup.times:
        if lo + _EPS < t < hi - _EPS:
            cuts.add(t)
    cuts_sorted = sorted(cuts)
    # 浮点防护：首尾强制钉死在交集边界。
    cuts_sorted[0], cuts_sorted[-1] = lo, hi

    raw_unknown: list[Interval] = []
    deviation_segments: list[OverSegment] = []

    for seg_lo, seg_hi in zip(cuts_sorted, cuts_sorted[1:]):
        if seg_hi - seg_lo <= _EPS:
            continue
        mid = (seg_lo + seg_hi) / 2.0
        ip = primary.pair_index_at(mid)
        ib = backup.pair_index_at(mid)
        # 任一侧在该段处断档（或无有效读数对）：交集这一部分状态未知，
        # 不插值、不外推，偏差区间也不得跨越。
        if ip is None or ib is None:
            raw_unknown.append(Interval(seg_lo, seg_hi))
            continue
        ap, bp = primary.pts[ip], primary.pts[ip + 1]
        ab, bb = backup.pts[ib], backup.pts[ib + 1]
        if is_sample_gap(
            ap, bp, primary_max_sample_gap_seconds
        ) or is_sample_gap(ab, bb, backup_max_sample_gap_seconds):
            raw_unknown.append(Interval(seg_lo, seg_hi))
            continue

        deviation_segments.extend(
            _deviation_pieces(
                seg_lo,
                seg_hi,
                primary,
                backup,
                ip,
                ib,
                allowed_temperature_difference,
            )
        )

    # 未知小段相邻即合并（切点恰好落在断档边界时会把同一断档切成多段）。
    unknown = _merge_intervals(raw_unknown)

    # 偏差段只在公共端点两侧都严格超阈时合并；未知段夹在中间天然断开，
    # 因为偏差段不会在未知段两端相接（中间隔着未知段）。
    merged_pieces: list[Interval] = []
    for seg in deviation_segments:
        if seg.end - seg.start <= _EPS:
            continue
        if (
            merged_pieces
            and abs(seg.start - merged_pieces[-1].end) < _EPS
            and seg.over_at_start
        ):
            merged_pieces[-1] = Interval(merged_pieces[-1].start, seg.end)
        else:
            merged_pieces.append(Interval(seg.start, seg.end))

    deviations = [
        iv
        for iv in merged_pieces
        if iv.end - iv.start + _EPS >= min_deviation_seconds
    ]

    return DeviationResult(
        deviation_intervals=deviations,
        unknown_intervals=unknown,
        deviation_seconds=sum(iv.end - iv.start for iv in deviations),
        unknown_seconds=sum(iv.end - iv.start for iv in unknown),
    )
