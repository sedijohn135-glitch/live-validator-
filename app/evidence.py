"""Live evidence at the zone: the only thing the validator judges (docs/VALIDATOR.md §2).

Signals are independent and cheap. The verdict is a balance — two confirmations, one of them
structural — never a single tick and never a six-step checklist.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.market import Candle, swing_high_indices, swing_low_indices
from app.setup_model import Setup

PRIMARY = ("RECLAIM", "REJECTION", "SHIFT")
WEIGHTS = {"RECLAIM": 2, "REJECTION": 2, "SHIFT": 2, "MOMENTUM": 1, "ABSORPTION": 1}
SCORE_MIN = 3

WINDOW_BARS = 30  # how far back from the touch evidence is read
RECLAIM_BARS = 3
WICK_SHARE = 0.55
MOMENTUM_BODY_ATR = 0.9
ABSORPTION_BARS = 3
KNIFE_ATR = 2.5
KNIFE_BARS = 3
SPREAD_SPIKE_MULT = 3.0
SPREAD_SPIKE_ATR = 0.5
QUOTE_MAX_AGE_S = 30.0
LATE_ADVANCE_R = 0.35

HOLD_TEXTS = {
    "FRESH": "ende pa qiri M1 të mbyllur pas prekjes",
    "DATA": "çmimi live mungon ose është i vjetër",
    "SPREAD": "spread i lartë — hyrja do ta paguante spike-un",
    "KNIFE": "çmimi po bie/ngjitet me forcë përmes zonës — pa ndalesë s'ka konfirmim",
}


@dataclass
class Signal:
    code: str
    detail: str = ""

    @property
    def weight(self) -> int:
        return WEIGHTS.get(self.code, 0)

    @property
    def is_primary(self) -> bool:
        return self.code in PRIMARY


@dataclass
class Verdict:
    signals: list[Signal] = field(default_factory=list)
    holds: list[str] = field(default_factory=list)
    extreme: float = 0.0
    advance_r: float = 0.0
    bars: int = 0

    @property
    def score(self) -> int:
        return sum(s.weight for s in self.signals)

    @property
    def has_primary(self) -> bool:
        return any(s.is_primary for s in self.signals)

    @property
    def confirmed(self) -> bool:
        return self.score >= SCORE_MIN and self.has_primary

    @property
    def ready(self) -> bool:
        return self.confirmed and not self.holds

    @property
    def late(self) -> bool:
        return self.advance_r > LATE_ADVANCE_R

    @property
    def codes(self) -> list[str]:
        return [s.code for s in self.signals]

    def hold_texts(self) -> list[str]:
        return [HOLD_TEXTS.get(code, code) for code in self.holds]


def window(ctx, touch_ts: float, timeframe: str = "M1") -> list[Candle]:
    """Closed candles whose close falls at or after the touch, newest last."""
    seconds = 60 if timeframe == "M1" else 300
    bars = [c for c in ctx.candles(timeframe) if c.t + seconds >= touch_ts]
    return bars[-WINDOW_BARS:]


def _touched(setup: Setup, candle: Candle) -> bool:
    return candle.l <= setup.zone_high and candle.h >= setup.zone_low


def _reclaim(setup: Setup, bars: list[Candle]) -> Signal | None:
    """Liquidity taken beyond the zone, then price closes back in — the trap that failed."""
    for i, bar in enumerate(bars):
        swept = bar.l < setup.zone_low if setup.is_long else bar.h > setup.zone_high
        if not swept:
            continue
        for later in bars[i : i + RECLAIM_BARS + 1]:
            back = later.c >= setup.zone_low if setup.is_long else later.c <= setup.zone_high
            if back:
                return Signal("RECLAIM", "likuiditeti u mor dhe çmimi u kthye brenda zonës")
    return None


def _rejection(setup: Setup, bars: list[Candle]) -> Signal | None:
    previous: Candle | None = None
    for bar in bars:
        if _touched(setup, bar):
            span = bar.span
            if span > 0:
                wick = (min(bar.o, bar.c) - bar.l) if setup.is_long else (bar.h - max(bar.o, bar.c))
                closed_out = bar.c > setup.zone_high if setup.is_long else bar.c < setup.zone_low
                if closed_out and wick / span >= WICK_SHARE:
                    return Signal("REJECTION", "qiri me bisht refuzimi dhe mbyllje jashtë zonës")
            if previous is not None:
                engulf = (
                    setup.is_long and bar.bullish and previous.bearish and bar.c > previous.h
                ) or (not setup.is_long and bar.bearish and previous.bullish and bar.c < previous.l)
                if engulf:
                    return Signal("REJECTION", "qiri gëlltitës te zona")
        previous = bar
    return None


def _shift(setup: Setup, bars: list[Candle]) -> Signal | None:
    """Micro market-structure shift: a close beyond the last opposing micro-swing."""
    if len(bars) < 5:
        return None
    indices = swing_high_indices(bars) if setup.is_long else swing_low_indices(bars)
    for index in reversed(indices):
        level = bars[index].h if setup.is_long else bars[index].l
        for bar in bars[index + 1 :]:
            broke = bar.c > level if setup.is_long else bar.c < level
            if broke:
                return Signal("SHIFT", "struktura mikro u thye në drejtimin e setupit")
        break
    return None


def _momentum(setup: Setup, bars: list[Candle], atr: float | None) -> Signal | None:
    if not atr:
        return None
    for bar in bars:
        aligned = bar.bullish if setup.is_long else bar.bearish
        if aligned and bar.body >= MOMENTUM_BODY_ATR * atr:
            return Signal("MOMENTUM", "qiri me trup të fortë në drejtimin e setupit")
    return None


def _absorption(setup: Setup, bars: list[Candle]) -> Signal | None:
    run = 0
    for bar in bars:
        if not _touched(setup, bar):
            run = 0
            continue
        broke = bar.c < setup.zone_low if setup.is_long else bar.c > setup.zone_high
        run = 0 if broke else run + 1
        if run >= ABSORPTION_BARS:
            return Signal("ABSORPTION", f"{ABSORPTION_BARS} qirinj M1 pa e humbur zonën")
    return None


def holds_for(setup: Setup, ctx, bars: list[Candle]) -> list[str]:
    holds: list[str] = []
    if not bars:
        holds.append("FRESH")
    if not ctx.data_ok or ctx.bid is None or ctx.quote_synthetic or ctx.quote_age > QUOTE_MAX_AGE_S:
        holds.append("DATA")
    atr = ctx.atr("M1") or 0.0
    spread = ctx.spread
    if spread is not None:
        cap = max(SPREAD_SPIKE_MULT * ctx.median_spread(), SPREAD_SPIKE_ATR * atr)
        if cap > 0 and spread > cap:
            holds.append("SPREAD")
    if atr and len(bars) >= KNIFE_BARS:
        recent = bars[-KNIFE_BARS:]
        adverse = (recent[0].o - recent[-1].c) if setup.is_long else (recent[-1].c - recent[0].o)
        if adverse > KNIFE_ATR * atr:
            holds.append("KNIFE")
    return holds


def advance_r(setup: Setup, price: float, risk: float) -> float:
    """How far price has already run from the zone toward the target, in R."""
    if risk <= 0:
        return 0.0
    gone = (price - setup.zone_high) if setup.is_long else (setup.zone_low - price)
    return max(0.0, gone / risk)


def extreme_since(setup: Setup, bars: list[Candle], fallback: float) -> float:
    if not bars:
        return fallback
    return min(b.l for b in bars) if setup.is_long else max(b.h for b in bars)


def evaluate(setup: Setup, ctx, touch_ts: float) -> Verdict:
    """The whole verdict for one moment: what the market has shown since the zone was touched."""
    bars = window(ctx, touch_ts)
    atr = ctx.atr("M1")
    signals = [
        signal
        for signal in (
            _reclaim(setup, bars),
            _rejection(setup, bars),
            _shift(setup, bars),
            _momentum(setup, bars, atr),
            _absorption(setup, bars),
        )
        if signal is not None
    ]
    price = ctx.bid if ctx.bid is not None else setup.zone_mid
    return Verdict(
        signals=signals,
        holds=holds_for(setup, ctx, bars),
        extreme=extreme_since(setup, bars, setup.zone_low if setup.is_long else setup.zone_high),
        advance_r=advance_r(setup, price, setup.risk),
        bars=len(bars),
    )
