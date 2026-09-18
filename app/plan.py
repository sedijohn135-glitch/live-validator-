"""Entry, stop, targets and the profit-securing level, recomputed from live data.

Structure gives the level, ATR gives the room (docs/VALIDATOR.md §4 and §5). Nothing here decides
whether to enter — that is `app/evidence.py`. This module only answers "at what prices".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.market import Candle, swing_high_indices, swing_low_indices
from app.setup_model import Setup

SL_BUFFER_ATR = 1.5  # beyond the structural extreme, so a normal wick cannot take the stop
SL_BUFFER_SPREAD = 2.0
SL_BUFFER_TICKS = 2.0
MIN_R_FRACTION = 0.35  # a new stop is never tighter than this share of the original R
SECURE_MIN_R = 0.5
SECURE_MAX_R = 1.0
ROUND_GRID = {"XAUUSD": 5.0, "BTCUSD": 250.0}

SESSION_LEVEL_KEYS = (
    "pdh",
    "pdl",
    "pwh",
    "pwl",
    "asian_high",
    "asian_low",
    "london_high",
    "london_low",
    "ny_midnight_open",
    "six_am_open",
)


@dataclass
class Plan:
    mode: str  # "MARKET" or "LIMIT"
    entry: float
    stop: float
    targets: list[float]
    risk: float
    secure_at: float
    secure_why: str
    rr: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "entry": self.entry,
            "stop": self.stop,
            "targets": list(self.targets),
            "risk": self.risk,
            "secure_at": self.secure_at,
            "secure_why": self.secure_why,
            "rr": list(self.rr),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Plan:
        return cls(
            mode=raw["mode"],
            entry=float(raw["entry"]),
            stop=float(raw["stop"]),
            targets=[float(t) for t in raw.get("targets", [])],
            risk=float(raw["risk"]),
            secure_at=float(raw["secure_at"]),
            secure_why=raw.get("secure_why", ""),
            rr=[float(v) for v in raw.get("rr", [])],
            notes=list(raw.get("notes", [])),
        )


def stop_buffer(ctx) -> float:
    """The room a normal wick needs: ATR first, spread and tick as floors."""
    atr = ctx.atr("M1") or 0.0
    return max(
        SL_BUFFER_ATR * atr,
        SL_BUFFER_SPREAD * ctx.median_spread(),
        SL_BUFFER_TICKS * ctx.sym.tick,
    )


def zone_pad(ctx) -> float:
    """How wide a zone to build around a single entry price."""
    atr5 = ctx.atr("M5") or 0.0
    return max(2 * ctx.median_spread(), 0.15 * atr5, 2 * ctx.sym.tick)


def structural_stop(setup: Setup, extreme: float, ctx) -> float:
    buffer = stop_buffer(ctx)
    return extreme - buffer if setup.is_long else extreme + buffer


def clamp_stop(setup: Setup, entry: float, candidate: float) -> tuple[float, list[str]]:
    """Never wider than the idea's stop, never tighter than 0.35 R of it.

    For a LONG the stop must sit between the submitted stop (the outer bound) and `floor` (the
    tightest it may be); for a SHORT the two swap sides. When the submitted stop is already tighter
    than the floor, it wins — the owner's invalidation is never widened by us.
    """
    notes: list[str] = []
    floor = entry - MIN_R_FRACTION * setup.risk if setup.is_long else entry + MIN_R_FRACTION * setup.risk
    outer, inner = (setup.stop_loss, floor) if setup.is_long else (floor, setup.stop_loss)
    if outer > inner:
        return setup.stop_loss, notes
    stop = min(max(candidate, outer), inner)
    if stop != candidate:
        wider = (candidate < setup.stop_loss) if setup.is_long else (candidate > setup.stop_loss)
        notes.append(
            "stopi strukturor ishte më i gjerë se ai i setupit — u mbajt stopi origjinal"
            if wider
            else "stopi strukturor ishte shumë i ngushtë — u zgjerua te 0.35R"
        )
    return stop, notes


def round_levels(symbol: str, low: float, high: float) -> list[float]:
    grid = ROUND_GRID.get(symbol.upper())
    if not grid or high <= low:
        return []
    first = (int(low / grid) + 1) * grid
    out = []
    level = first
    while level < high and len(out) < 40:
        out.append(round(level, 6))
        level += grid
    return out


def swing_levels(candles: list[Candle], want_high: bool, limit: int = 6) -> list[float]:
    if len(candles) < 5:
        return []
    indices = swing_high_indices(candles) if want_high else swing_low_indices(candles)
    return [candles[i].h if want_high else candles[i].l for i in indices[-limit:]]


def secure_level(setup: Setup, entry: float, stop: float, ctx) -> tuple[float, str]:
    """The nearest real obstacle between the entry and TP1 — where price can turn before TP1.

    Order of preference: opposing swings, then session levels, then round numbers, capped at 1 R.
    """
    risk = abs(entry - stop) or setup.risk
    target = setup.tp1 or (entry + risk * 3 if setup.is_long else entry - risk * 3)
    near = entry + SECURE_MIN_R * risk if setup.is_long else entry - SECURE_MIN_R * risk
    far = entry + SECURE_MAX_R * risk if setup.is_long else entry - SECURE_MAX_R * risk
    if setup.is_long:
        window = (min(near, target), min(far, target))
    else:
        window = (max(far, target), max(near, target))
    lo, hi = min(window), max(window)

    candidates: list[tuple[float, str]] = []
    for timeframe in ("M5", "M15"):
        for level in swing_levels(ctx.candles(timeframe), want_high=setup.is_long):
            candidates.append((level, f"swing {timeframe}"))
    for key in SESSION_LEVEL_KEYS:
        level = ctx.levels.get(key)
        if level:
            candidates.append((float(level), key.upper()))
    for level in round_levels(setup.symbol, lo, hi):
        candidates.append((level, "numër i rrumbullakët"))

    inside = [(level, why) for level, why in candidates if lo <= level <= hi]
    if inside:
        inside.sort(key=lambda item: abs(item[0] - entry))
        return inside[0]
    return (far, "1R")


def targets_for(setup: Setup, entry: float) -> list[float]:
    """Keep the idea's targets, minus any the price has already passed."""
    return [t for t in setup.targets if setup.ahead(entry, t)]


def rr_for(entry: float, stop: float, targets: list[float]) -> list[float]:
    risk = abs(entry - stop)
    if risk <= 0:
        return []
    return [round(abs(t - entry) / risk, 2) for t in targets]


def limit_price(setup: Setup, candles: list[Candle], price: float, ctx) -> tuple[float, str]:
    """Where to place the pullback order when the move left without us (§3).

    The first of: the confirmation candle's midpoint, the M1 fair value gap it created, the zone edge.
    """
    edge = setup.entry_edge
    between = []
    if candles:
        last = candles[-1]
        between.append(((last.h + last.l) / 2, "50% i qiriut të konfirmimit"))
    if len(candles) >= 3:
        first, _mid, third = candles[-3], candles[-2], candles[-1]
        if setup.is_long and third.l > first.h:
            between.append(((first.h + third.l) / 2, "FVG M1"))
        if not setup.is_long and third.h < first.l:
            between.append(((first.l + third.h) / 2, "FVG M1"))
    for level, why in between:
        if setup.is_long and edge <= level < price:
            return level, why
        if not setup.is_long and price < level <= edge:
            return level, why
    return edge, "buza e zonës"


def in_premium_half(setup: Setup, price: float) -> bool:
    """A LONG filled above the middle of its own demand zone is buying the expensive edge of it.

    Only while price is still inside the zone. Once it has left, the move is under way and the
    distance from the zone decides between a market fill and a pullback order.
    """
    if not setup.zone_low <= price <= setup.zone_high:
        return False
    return price > setup.zone_mid if setup.is_long else price < setup.zone_mid


def discount_entry(setup: Setup, candles: list[Candle], price: float, ctx) -> tuple[float, str]:
    """Where to wait instead of paying the premium half: never worse than the middle of the zone."""
    level, why = limit_price(setup, candles, price, ctx)
    middle = setup.zone_mid
    if setup.is_long:
        return (level, why) if level <= middle else (middle, "gjysma e lirë e zonës")
    return (level, why) if level >= middle else (middle, "gjysma e lirë e zonës")


def build_plan(setup: Setup, mode: str, entry: float, extreme: float, ctx, notes: list[str] | None = None) -> Plan:
    stop, clamp_notes = clamp_stop(setup, entry, structural_stop(setup, extreme, ctx))
    targets = targets_for(setup, entry)
    secure_at, secure_why = secure_level(setup, entry, stop, ctx)
    return Plan(
        mode=mode,
        entry=entry,
        stop=stop,
        targets=targets,
        risk=abs(entry - stop),
        secure_at=secure_at,
        secure_why=secure_why,
        rr=rr_for(entry, stop, targets),
        notes=(notes or []) + clamp_notes,
    )
