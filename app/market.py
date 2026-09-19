"""Candle primitives: ATR, swings, FVGs, session levels and the in-memory candle store.

Everything here is pure and testable; nothing talks to the network (validation-rules §1).
"""

from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass
from datetime import timedelta

from app.config import TIMEFRAMES
from app.timeutil import NY, candle_is_closed, from_epoch, ny_datetime, ny_string, to_ny


@dataclass(frozen=True)
class Candle:
    t: float  # open time, epoch seconds
    o: float
    h: float
    l: float  # noqa: E741 - matches the OHLC vocabulary used throughout the rules
    c: float

    @property
    def body_top(self) -> float:
        return max(self.o, self.c)

    @property
    def body_bot(self) -> float:
        return min(self.o, self.c)

    @property
    def body(self) -> float:
        return self.body_top - self.body_bot

    @property
    def span(self) -> float:
        return self.h - self.l

    @property
    def bullish(self) -> bool:
        return self.c > self.o

    @property
    def bearish(self) -> bool:
        return self.c < self.o

    def as_list(self, decimals: int) -> list:
        return [
            ny_string(self.t),
            round(self.o, decimals),
            round(self.h, decimals),
            round(self.l, decimals),
            round(self.c, decimals),
        ]


@dataclass(frozen=True)
class FVG:
    direction: str  # BULL | BEAR
    low: float
    high: float
    formed_at: float  # open time of the middle candle
    kind: str  # wick | body
    index: int  # index of the middle candle in the series it was found in

    @property
    def ce(self) -> float:
        return (self.low + self.high) / 2.0

    @property
    def width(self) -> float:
        return self.high - self.low


def true_range(current: Candle, previous: Candle | None) -> float:
    if previous is None:
        return current.span
    return max(current.span, abs(current.h - previous.c), abs(current.l - previous.c))


def atr14(candles: list[Candle], period: int = 14) -> float | None:
    """Mean of the last `period` true ranges of closed candles. `None` when there is not enough data."""
    if len(candles) < period + 1:
        return None
    ranges = [true_range(candles[i], candles[i - 1]) for i in range(len(candles) - period, len(candles))]
    return sum(ranges) / period


AO_FAST, AO_SLOW = 5, 34  # Awesome Oscillator: SMA5 - SMA34 of the median price


def awesome_oscillator(candles: list[Candle]) -> list[float]:
    """AO over the median price, aligned to `candles[AO_SLOW - 1:]`.

    Computed here rather than left to the model: it is exactly determined by the candles, and a
    34-period mean worked out in prose is a 34-period mean worked out wrong.
    """
    if len(candles) < AO_SLOW:
        return []
    medians = [(bar.h + bar.l) / 2 for bar in candles]
    out = []
    for i in range(AO_SLOW - 1, len(medians)):
        fast = sum(medians[i - AO_FAST + 1 : i + 1]) / AO_FAST
        slow = sum(medians[i - AO_SLOW + 1 : i + 1]) / AO_SLOW
        out.append(fast - slow)
    return out


def ao_divergence(candles: list[Candle], tolerance: float = 0.0) -> tuple[str, str]:
    """Which way the oscillator disagrees with price, comparing the last two swings.

    SELL: price made a higher high and AO did not follow. BUY: a lower low and AO did not follow.
    Returns ("SELL" | "BUY" | "", a sentence naming the two prices).
    """
    values = awesome_oscillator(candles)
    if len(values) < 6:
        return "", ""
    aligned = candles[len(candles) - len(values) :]
    highs = swing_high_indices(aligned)
    if len(highs) >= 2:
        first, second = highs[-2], highs[-1]
        if aligned[second].h >= aligned[first].h + tolerance and values[second] < values[first]:
            return "SELL", f"maja {aligned[first].h:.2f} → {aligned[second].h:.2f}, AO më poshtë"
    lows = swing_low_indices(aligned)
    if len(lows) >= 2:
        first, second = lows[-2], lows[-1]
        if aligned[second].l <= aligned[first].l - tolerance and values[second] > values[first]:
            return "BUY", f"fundi {aligned[first].l:.2f} → {aligned[second].l:.2f}, AO më lart"
    return "", ""


def swing_high_indices(candles: list[Candle]) -> list[int]:
    """Strict swing highs (v11 §1.2): `h[i] > h[i-1]` and `h[i] > h[i+1]`."""
    return [
        i for i in range(1, len(candles) - 1) if candles[i].h > candles[i - 1].h and candles[i].h > candles[i + 1].h
    ]


def swing_low_indices(candles: list[Candle]) -> list[int]:
    return [
        i for i in range(1, len(candles) - 1) if candles[i].l < candles[i - 1].l and candles[i].l < candles[i + 1].l
    ]


def find_fvgs(candles: list[Candle], start: int = 0) -> list[FVG]:
    """All wick- and body-FVGs with a middle candle at index >= start + 1."""
    found: list[FVG] = []
    for m in range(max(1, start + 1), len(candles) - 1):
        prev, nxt = candles[m - 1], candles[m + 1]
        if nxt.l > prev.h:
            found.append(FVG("BULL", prev.h, nxt.l, candles[m].t, "wick", m))
        elif nxt.body_bot > prev.body_top:
            found.append(FVG("BULL", prev.body_top, nxt.body_bot, candles[m].t, "body", m))
        if nxt.h < prev.l:
            found.append(FVG("BEAR", nxt.h, prev.l, candles[m].t, "wick", m))
        elif nxt.body_top < prev.body_bot:
            found.append(FVG("BEAR", nxt.body_top, prev.body_bot, candles[m].t, "body", m))
    return found


def fvg_status(fvg: FVG, candles: list[Candle]) -> str:
    """untouched | tapped | ce_breached | failed | inverted, judged on candles after m+1."""
    later = candles[fvg.index + 2 :]
    if not later:
        return "untouched"
    far = fvg.low if fvg.direction == "BULL" else fvg.high
    failed_at: int | None = None
    for i, candle in enumerate(later):
        beyond = candle.c < far if fvg.direction == "BULL" else candle.c > far
        if beyond:
            failed_at = i
            break
    if failed_at is not None:
        for candle in later[failed_at + 1 :]:
            if candle.h >= fvg.low and candle.l <= fvg.high:
                return "inverted"
        return "failed"
    touched = any(c.h >= fvg.low and c.l <= fvg.high for c in later)
    if not touched:
        return "untouched"
    breached = any(c.l < fvg.ce for c in later) if fvg.direction == "BULL" else any(c.h > fvg.ce for c in later)
    return "ce_breached" if breached else "tapped"


def efficiency_ratio(candles: list[Candle], length: int = 24) -> float | None:
    """ER over the last `length` closes (validation-rules §1)."""
    if len(candles) < length + 1:
        return None
    window = candles[-(length + 1) :]
    net = abs(window[-1].c - window[0].c)
    path = sum(abs(window[i].c - window[i - 1].c) for i in range(1, len(window)))
    if path == 0:
        return 0.0
    return net / path


def displacement(candle: Candle, atr: float, body_atr: float, body_range: float) -> bool:
    if atr is None or atr <= 0 or candle.span <= 0:
        return False
    return candle.body >= body_atr * atr and candle.body / candle.span >= body_range


def tolerance(atr: float | None, median_spread: float, tick: float, level_tol_atr: float) -> float:
    """tol(tf) = max(LEVEL_TOL_ATR × ATR14(tf), 2 × median_spread_60m, 3 × tick)."""
    atr_part = level_tol_atr * atr if atr else 0.0
    return max(atr_part, 2.0 * median_spread, 3.0 * tick)


def median_spread(samples: list[float], fallback_max_spread: float) -> float:
    """Median of the last 60 min of spread samples; fewer than 30 samples ⇒ MAX_SPREAD_ABS / 3."""
    if len(samples) < 30:
        return fallback_max_spread / 3.0
    return statistics.median(samples)


class CandleStore:
    """Per (symbol, timeframe) series of closed candles, merged by open timestamp."""

    def __init__(self, close_grace_s: float = 2.0) -> None:
        self._series: dict[tuple[str, str], list[Candle]] = {}
        self.close_grace_s = close_grace_s

    def series(self, symbol: str, timeframe: str) -> list[Candle]:
        return self._series.get((symbol.upper(), timeframe.upper()), [])

    def merge(self, symbol: str, timeframe: str, candles: list[Candle], now_ts: float, keep: int = 1500) -> int:
        """Merge candles, dropping any bar that is not closed yet. Returns the number of new bars."""
        tf_seconds = TIMEFRAMES[timeframe.upper()]
        key = (symbol.upper(), timeframe.upper())
        current = self._series.setdefault(key, [])
        index = {c.t: i for i, c in enumerate(current)}
        added = 0
        for candle in candles:
            if not candle_is_closed(candle.t, tf_seconds, now_ts, self.close_grace_s):
                continue
            existing = index.get(candle.t)
            if existing is None:
                current.append(candle)
                added += 1
            else:
                current[existing] = candle
        current.sort(key=lambda c: c.t)
        if len(current) > keep:
            del current[: len(current) - keep]
        self._series[key] = current
        return added

    def last(self, symbol: str, timeframe: str) -> Candle | None:
        series = self.series(symbol, timeframe)
        return series[-1] if series else None

    def closed_before(self, symbol: str, timeframe: str, close_ts: float) -> list[Candle]:
        """Candles whose close time is <= `close_ts`."""
        tf_seconds = TIMEFRAMES[timeframe.upper()]
        return [c for c in self.series(symbol, timeframe) if c.t + tf_seconds <= close_ts]

    def since(self, symbol: str, timeframe: str, open_ts: float) -> list[Candle]:
        series = self.series(symbol, timeframe)
        starts = [c.t for c in series]
        return series[bisect.bisect_left(starts, open_ts) :]

    def missing_between(self, symbol: str, timeframe: str, start_ts: float, end_ts: float) -> int:
        """Count of absent bars between two open timestamps (used by T-08)."""
        tf_seconds = TIMEFRAMES[timeframe.upper()]
        have = {c.t for c in self.series(symbol, timeframe)}
        expected = 0
        missing = 0
        ts = start_ts
        while ts <= end_ts and expected < 5000:
            expected += 1
            if ts not in have:
                missing += 1
            ts += tf_seconds
        return missing

    def atr(self, symbol: str, timeframe: str) -> float | None:
        return atr14(self.series(symbol, timeframe))


def session_levels(store: CandleStore, symbol: str, now_ts: float) -> dict[str, float | None]:
    """Asian/London ranges, NY midnight & 6 AM opens, PDH/PDL, PWH/PWL, lookback extremes."""
    now = from_epoch(now_ts)
    ny_now = to_ny(now)
    today = ny_now.date()
    m5 = store.series(symbol, "M5")

    def m5_range(start_dt, end_dt) -> tuple[float | None, float | None]:
        lo: float | None = None
        hi: float | None = None
        start_ts, end_ts = start_dt.timestamp(), end_dt.timestamp()
        for candle in m5:
            if start_ts <= candle.t < end_ts:
                lo = candle.l if lo is None else min(lo, candle.l)
                hi = candle.h if hi is None else max(hi, candle.h)
        return hi, lo

    def m5_open_at(dt) -> float | None:
        target = dt.timestamp()
        for candle in m5:
            if candle.t == target:
                return candle.o
        return None

    asian_start = ny_datetime(today - timedelta(days=1), 19 * 60)
    asian_end = ny_datetime(today, 0)
    if ny_now < asian_end:  # before midnight the Asian session of the current NY day is still forming
        asian_start = ny_datetime(today, 19 * 60)
        asian_end = ny_datetime(today + timedelta(days=1), 0)
    asian_high, asian_low = m5_range(asian_start, asian_end)
    london_high, london_low = m5_range(ny_datetime(today, 2 * 60), ny_datetime(today, 5 * 60))

    d1 = store.series(symbol, "D1")
    w1 = store.series(symbol, "W1")
    pdh = d1[-1].h if d1 else None
    pdl = d1[-1].l if d1 else None
    pwh = w1[-1].h if w1 else None
    pwl = w1[-1].l if w1 else None
    lookback = d1[-250:]
    return {
        "asian_high": asian_high,
        "asian_low": asian_low,
        "london_high": london_high,
        "london_low": london_low,
        "ny_midnight_open": m5_open_at(ny_datetime(today, 0)),
        "six_am_open": m5_open_at(ny_datetime(today, 6 * 60)),
        "pdh": pdh,
        "pdl": pdl,
        "pwh": pwh,
        "pwl": pwl,
        "lookback_high": max((c.h for c in lookback), default=None),
        "lookback_low": min((c.l for c in lookback), default=None),
    }


def swings_for_snapshot(store: CandleStore, symbol: str, timeframe: str, count: int = 5) -> dict[str, list]:
    series = store.series(symbol, timeframe)
    highs = [[ny_string(series[i].t), series[i].h] for i in swing_high_indices(series)][-count:]
    lows = [[ny_string(series[i].t), series[i].l] for i in swing_low_indices(series)][-count:]
    return {"highs": highs, "lows": lows}


def ny_day_start(now_ts: float) -> float:
    ny_now = to_ny(from_epoch(now_ts))
    return ny_datetime(ny_now.date(), 0).timestamp()


__all__ = [
    "NY",
    "Candle",
    "CandleStore",
    "FVG",
    "atr14",
    "displacement",
    "efficiency_ratio",
    "find_fvgs",
    "fvg_status",
    "median_spread",
    "ny_day_start",
    "session_levels",
    "swing_high_indices",
    "swing_low_indices",
    "swings_for_snapshot",
    "tolerance",
    "true_range",
]


SNAPSHOT_COUNTS = {"D1": 30, "H4": 60, "H1": 72, "M15": 96, "M5": 96, "M1": 60}
SNAPSHOT_FVG_TFS = ("M5", "M15", "H1", "H4")
SNAPSHOT_SWING_TFS = ("M15", "H1")


SNAPSHOT_AO_TFS = ("M15", "M5")  # the two the strategy reads the divergence on
AO_SNAPSHOT_VALUES = 10


def snapshot_ao(store: CandleStore, symbol: str, timeframe: str, decimals: int) -> dict:
    """The oscillator, already computed, plus the divergence it shows.

    The model should never be asked to work out a 34-period mean in prose: the value is exactly
    determined by the candles, so the server produces it and the analysis reads it.
    """
    candles = store.series(symbol, timeframe)
    values = awesome_oscillator(candles)
    if not values:
        return {"values": [], "divergence": None, "detail": "", "bars": len(candles)}
    side, detail = ao_divergence(candles)
    return {
        "values": [round(v, decimals) for v in values[-AO_SNAPSHOT_VALUES:]],
        "last": round(values[-1], decimals),
        "divergence": side or None,
        "detail": detail,
    }


def snapshot_fvgs(store: CandleStore, symbol: str, timeframe: str, decimals: int, limit: int = 6) -> list[dict]:
    """The last `limit` arrays of a timeframe, dropping failed ones unless they inverted."""
    series = store.series(symbol, timeframe)
    if len(series) < 3:
        return []
    out: list[dict] = []
    for fvg in find_fvgs(series):
        status = fvg_status(fvg, series)
        if status == "failed":
            continue
        out.append(
            {
                "dir": "BULL" if fvg.direction == "BULL" else "BEAR",
                "low": round(fvg.low, decimals),
                "high": round(fvg.high, decimals),
                "ce": round(fvg.ce, decimals),
                "formed_at": ny_string(fvg.formed_at),
                "kind": fvg.kind,
                "status": status,
            }
        )
    return out[-limit:]


def build_snapshot(
    symbol: str,
    store: CandleStore,
    now_ts: float,
    decimals: int,
    quote: dict | None,
    levels: dict,
    time_block: dict,
    data: dict | None = None,
) -> dict:
    """The `market_snapshot` payload (mcp-oauth §6). Compact by construction: fixed counts, flat arrays."""
    atr = {tf: store.atr(symbol, tf) for tf in ("M5", "M15", "H1", "D1")}
    notes = [
        "times are New York",
        "candle time = open time",
        "send the setup with entry/zone, stop and targets — nothing else is required",
    ]
    if data is not None and not data.get("usable", True):
        notes.insert(
            0,
            "NO MARKET DATA: do not analyse and do not call setup_submit. Tell the owner to run "
            "/selftest on Telegram.",
        )
    return {
        "schema": "snapshot/1",
        "symbol": symbol,
        "source": "IC Markets cTrader (bid candles)",
        "data": data or {"status": "unknown", "usable": True},
        "time": time_block,
        "quote": quote,
        "levels": {k: (round(v, decimals) if isinstance(v, (int, float)) else v) for k, v in levels.items()},
        "atr": {k: (round(v, decimals) if v else None) for k, v in atr.items()},
        "ao": {tf: snapshot_ao(store, symbol, tf, decimals) for tf in SNAPSHOT_AO_TFS},
        "swings": {tf: swings_for_snapshot(store, symbol, tf) for tf in SNAPSHOT_SWING_TFS},
        "fvgs": {tf: snapshot_fvgs(store, symbol, tf, decimals) for tf in SNAPSHOT_FVG_TFS},
        "candles": {
            tf: [c.as_list(decimals) for c in store.series(symbol, tf)[-count:]]
            for tf, count in SNAPSHOT_COUNTS.items()
        },
        "notes": notes,
    }
