"""Live evidence at the zone: the only thing the validator judges (docs/VALIDATOR.md §2).

Every signal must clear a materiality floor tied to live volatility. Without one, ordinary chop
inside a zone confirms itself: a one-tick dip below the edge is not a liquidity sweep, a doji is not
a rejection, and a two-tick swing is not structure. The verdict is still a balance — two independent
confirmations, one of them structural — but each one now has to be worth counting.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.market import Candle, swing_high_indices, swing_low_indices
from app.setup_model import Setup

# The owner's three confirmations. They are equals: any one of them is an entry, and each extra one
# makes the same entry stronger. Nothing here is a checklist — the analysis names the signs a model
# usually shows, but the market gives what it gives, and the engine counts what actually appeared.
CORE = ("ZONE_BREAK", "AO_DIV", "QUASIMODO")
STRENGTH_TEXT = {1: "konfirmim", 2: "konfirmim i fortë", 3: "konfirmim shumë i fortë"}

# Supporting evidence: never enough on its own, always worth reporting alongside a core signal.
PRIMARY = CORE
WEIGHTS = {
    "ZONE_BREAK": 2,
    "AO_DIV": 2,
    "QUASIMODO": 2,
    "RECLAIM": 1,
    "REJECTION": 1,
    "MOMENTUM": 1,
    "ABSORPTION": 1,
}
SCORE_MIN = 2  # one core confirmation; the score is reported, the strength is what decides

BREAK_TIMEFRAMES = ("M1", "M5", "M15")  # the nearest demand/supply can live on any of them
BREAK_BARS = 60

AO_FAST, AO_SLOW = 5, 34  # Awesome Oscillator: SMA5 − SMA34 of the median price
AO_MIN_BARS = AO_SLOW + 10

WINDOW_BARS = 30  # how far back from the touch evidence is read
RECLAIM_BARS = 3
ABSORPTION_BARS = 3
WICK_SHARE = 0.55  # a rejection candle's wick, as a share of its range
CLOSE_STRENGTH = 0.66  # a strong close sits in this fraction of the candle's range

# Materiality floors, all in multiples of ATR(M1) with the spread as the hard floor underneath.
MIN_SWEEP_ATR = 0.25  # a sweep must take real depth beyond the level
MIN_CLEAR_ATR = 0.20  # and price must close back past it by this much
MIN_CANDLE_ATR = 0.60  # a signal candle must be a real candle
MIN_BODY_ATR = 0.90  # a momentum candle's body
MIN_BREAK_ATR = 0.15  # a structure break must clear the swing by this much
MIN_SWING_ATR = 0.50  # and the swing it breaks must itself be this tall
MIN_REACTION_ATR = 0.50  # price must have left the extreme: nothing else proves a defence
FAILURE_ATR = 0.50  # closes this far beyond the far edge mean the zone is breaking
FAILURE_BARS = 2

KNIFE_ATR = 2.5
KNIFE_BARS = 3
SPREAD_SPIKE_MULT = 3.0
SPREAD_SPIKE_ATR = 0.5
QUOTE_MAX_AGE_S = 30.0
LATE_ADVANCE_R = 0.35

HOLD_TEXTS = {
    "FRESH": "ende pa qiri M1 të mbyllur pas prekjes",
    "DATA": "çmimi live ose shkalla e volatilitetit mungon",
    "SPREAD": "spread i lartë — hyrja do ta paguante spike-un",
    "KNIFE": "çmimi po bie/ngjitet me forcë përmes zonës — pa ndalesë s'ka konfirmim",
    "REACTION": "çmimi s'është larguar ende nga ekstremi — asgjë nuk u mbrojt",
    "CONFIRMATION": "asnjë nga tri konfirmimet: thyerje demand/supply, divergjencë AO, quasimodo",
}


@dataclass(frozen=True)
class Scale:
    """Live volatility and the smallest price difference worth calling a difference."""

    atr: float
    tol: float

    def of(self, factor: float) -> float:
        return max(factor * self.atr, self.tol)

    @property
    def usable(self) -> bool:
        return self.atr > 0


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
    reaction_atr: float = 0.0

    @property
    def score(self) -> int:
        return sum(s.weight for s in self.signals)

    @property
    def has_primary(self) -> bool:
        return any(s.is_primary for s in self.signals)

    @property
    def core(self) -> list[str]:
        """Which of the owner's three confirmations actually appeared."""
        return [s.code for s in self.signals if s.code in CORE]

    @property
    def strength(self) -> int:
        """1 is an entry, 2 is stronger, 3 is as strong as this engine can read."""
        return len(self.core)

    @property
    def strength_text(self) -> str:
        return STRENGTH_TEXT.get(self.strength, "pa konfirmim")

    @property
    def confirmed(self) -> bool:
        return self.strength >= 1

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


def _strong_close(setup: Setup, bar: Candle) -> bool:
    """A close at the working end of the candle, not in the middle of it."""
    if bar.span <= 0:
        return False
    position = (bar.c - bar.l) / bar.span if setup.is_long else (bar.h - bar.c) / bar.span
    return position >= CLOSE_STRENGTH


def _is_pin(setup: Setup, bar: Candle) -> bool:
    """A rejection candle: the wick did the work and the close held the other end."""
    if bar.span <= 0:
        return False
    wick = (min(bar.o, bar.c) - bar.l) if setup.is_long else (bar.h - max(bar.o, bar.c))
    return wick / bar.span >= WICK_SHARE and _strong_close(setup, bar)


def _liquidity_level(setup: Setup, prior: list[Candle]) -> float:
    """What a sweep has to take out: the zone edge, or the running extreme if price already went further.

    Dipping under the zone edge when price has already traded lower takes no liquidity at all — the
    stops there are long gone.
    """
    edge = setup.zone_low if setup.is_long else setup.zone_high
    if not prior:
        return edge
    extreme = min(b.l for b in prior) if setup.is_long else max(b.h for b in prior)
    return min(edge, extreme) if setup.is_long else max(edge, extreme)


def _reclaim(setup: Setup, bars: list[Candle], scale: Scale) -> Signal | None:
    """Liquidity taken with real depth, then price closes back past the level — the trap that failed."""
    depth_needed = scale.of(MIN_SWEEP_ATR)
    clear = scale.of(MIN_CLEAR_ATR)
    for i, bar in enumerate(bars):
        level = _liquidity_level(setup, bars[:i])
        depth = (level - bar.l) if setup.is_long else (bar.h - level)
        if depth < depth_needed:
            continue
        for offset, later in enumerate(bars[i : i + RECLAIM_BARS + 1]):
            reclaimed = (later.c >= level + clear) if setup.is_long else (later.c <= level - clear)
            if not reclaimed:
                continue
            if offset == 0 and not _is_pin(setup, later):
                continue  # a sweep and reclaim inside one candle only counts as a rejection candle
            return Signal("RECLAIM", f"likuiditeti te {level:.2f} u mor dhe çmimi u kthye përtej tij")
    return None


def _rejection(setup: Setup, bars: list[Candle], scale: Scale) -> Signal | None:
    """A real candle that refused the zone and closed outside it."""
    body_needed = scale.of(MIN_CANDLE_ATR)
    clear = scale.of(MIN_CLEAR_ATR)
    edge = setup.zone_high if setup.is_long else setup.zone_low
    previous: Candle | None = None
    for bar in bars:
        if _touched(setup, bar):
            closed_out = (bar.c >= edge + clear) if setup.is_long else (bar.c <= edge - clear)
            if closed_out and bar.span >= body_needed and _is_pin(setup, bar):
                return Signal("REJECTION", "qiri refuzimi me mbyllje bindëse jashtë zonës")
            if previous is not None and bar.body >= body_needed:
                engulf = (
                    setup.is_long and bar.bullish and previous.bearish and bar.c >= previous.h + clear
                ) or (not setup.is_long and bar.bearish and previous.bullish and bar.c <= previous.l - clear)
                if engulf:
                    return Signal("REJECTION", "qiri gëlltitës me mbyllje përtej qiriut të mëparshëm")
        previous = bar
    return None


def _break_on(setup: Setup, bars: list[Candle], scale: Scale) -> float | None:
    """The level of the nearest opposing zone broken on this series, or None if it still holds."""
    if len(bars) < 5:
        return None
    clear = scale.of(MIN_BREAK_ATR)
    height_needed = scale.of(MIN_SWING_ATR)
    indices = swing_high_indices(bars) if setup.is_long else swing_low_indices(bars)
    for index in reversed(indices):
        after = bars[index + 1 :]
        if not after:
            continue
        level = bars[index].h if setup.is_long else bars[index].l
        base = min(b.l for b in after) if setup.is_long else max(b.h for b in after)
        if abs(level - base) < height_needed:
            return None  # the most recent structure is noise, not a level anyone defends
        for bar in after:
            if (bar.c >= level + clear) if setup.is_long else (bar.c <= level - clear):
                return level
        return None  # the most recent real swing is still intact
    return None


def _zone_break(setup: Setup, ctx, scale: Scale) -> Signal | None:
    """The nearest opposing demand (short) or supply (long), broken.

    Read on M1, M5 and M15, because the nearest zone can live on any of them: a short into an M15
    supply is confirmed by the M15 demand giving way, and looking only at M1 would miss it. When
    more than one timeframe has broken, the one whose level sits nearest the entry zone is reported
    — that is the zone price actually had to get through.
    """
    found: list[tuple[float, str, float]] = []
    for timeframe in BREAK_TIMEFRAMES:
        level = _break_on(setup, ctx.candles(timeframe)[-BREAK_BARS:], scale)
        if level is not None:
            found.append((abs(level - setup.zone_mid), timeframe, level))
    if not found:
        return None
    _distance, timeframe, level = min(found)
    zone_word = "supply" if setup.is_long else "demand"
    others = len(found) - 1
    extra = f" (+{others} tjetër)" if others == 1 else f" (+{others} të tjera)" if others else ""
    return Signal("ZONE_BREAK", f"{zone_word} {timeframe} më i afërt u thye te {level:.2f}{extra}")


def _ao(bars: list[Candle]) -> list[float]:
    """Awesome Oscillator over the median price, aligned to `bars[AO_SLOW - 1:]`."""
    if len(bars) < AO_SLOW:
        return []
    medians = [(bar.h + bar.l) / 2 for bar in bars]
    out = []
    for i in range(AO_SLOW - 1, len(medians)):
        fast = sum(medians[i - AO_FAST + 1 : i + 1]) / AO_FAST
        slow = sum(medians[i - AO_SLOW + 1 : i + 1]) / AO_SLOW
        out.append(fast - slow)
    return out


def _ao_divergence(setup: Setup, series: list[Candle], scale: Scale) -> Signal | None:
    """Price made a new extreme and the oscillator did not: the push had nothing behind it.

    Read on the full M1 series, not the window since the touch — the oscillator needs 34 bars of
    history before it means anything, and the divergence usually forms before price reaches a zone.
    """
    values = _ao(series)
    if len(values) < 6:
        return None
    aligned = series[len(series) - len(values) :]
    indices = swing_low_indices(aligned) if setup.is_long else swing_high_indices(aligned)
    if len(indices) < 2:
        return None
    first, second = indices[-2], indices[-1]
    clear = scale.of(MIN_BREAK_ATR)
    if setup.is_long:
        if aligned[second].l <= aligned[first].l - clear and values[second] > values[first]:
            return Signal("AO_DIV", f"minimum më i ulët te {aligned[second].l:.2f}, AO jo — divergjencë blerjeje")
    elif aligned[second].h >= aligned[first].h + clear and values[second] < values[first]:
        return Signal("AO_DIV", f"maksimum më i lartë te {aligned[second].h:.2f}, AO jo — divergjencë shitjeje")
    return None


def _quasimodo(setup: Setup, series: list[Candle], scale: Scale) -> Signal | None:
    """Left shoulder, a higher head, the neckline broken, and price back at the shoulder.

    The confirmation that needs no oscillator: the head takes the liquidity above the shoulder, the
    break below the neckline says who won, and the return to the shoulder is where the entry lives.
    """
    if len(series) < 8:
        return None
    clear = scale.of(MIN_BREAK_ATR)
    indices = swing_low_indices(series) if setup.is_long else swing_high_indices(series)
    if len(indices) < 2:
        return None
    shoulder_i, head_i = indices[-2], indices[-1]
    between = series[shoulder_i : head_i + 1]
    after = series[head_i + 1 :]
    if not between or not after:
        return None
    if setup.is_long:
        shoulder, head = series[shoulder_i].l, series[head_i].l
        if head > shoulder - clear:
            return None  # no lower head: this is not a quasimodo
        neckline = max(bar.h for bar in between)
        broke = [i for i, bar in enumerate(after) if bar.c >= neckline + clear]
        if not broke:
            return None
        # The right shoulder is a return that happens AFTER the neckline gave way. A dip back to
        # the shoulder level before the break is just the pattern still forming.
        right = after[broke[0] + 1 :]
        back_at_shoulder = any(bar.l <= shoulder + clear for bar in right)
    else:
        shoulder, head = series[shoulder_i].h, series[head_i].h
        if head < shoulder + clear:
            return None
        neckline = min(bar.l for bar in between)
        broke = [i for i, bar in enumerate(after) if bar.c <= neckline - clear]
        if not broke:
            return None
        right = after[broke[0] + 1 :]
        back_at_shoulder = any(bar.h >= shoulder - clear for bar in right)
    if not back_at_shoulder:
        return None
    return Signal("QUASIMODO", f"quasimodo: koka mori {shoulder:.2f}, qafa u thye te {neckline:.2f}")


def _momentum(setup: Setup, bars: list[Candle], scale: Scale) -> Signal | None:
    """A big body that also closed at its extreme: an impulse, not a candle that gave it back."""
    for bar in bars:
        aligned = bar.bullish if setup.is_long else bar.bearish
        if aligned and bar.body >= MIN_BODY_ATR * scale.atr and _strong_close(setup, bar):
            return Signal("MOMENTUM", "qiri me trup të fortë dhe mbyllje në skaj")
    return None


def _absorption(setup: Setup, bars: list[Candle], scale: Scale) -> Signal | None:
    """The zone's better half held while it was actually being pressed."""
    mid = setup.zone_mid
    run = 0
    pressed = False
    for bar in bars:
        if not _touched(setup, bar):
            run, pressed = 0, False
            continue
        if setup.is_long:
            pressed = pressed or bar.l <= mid
            held = bar.c >= mid
        else:
            pressed = pressed or bar.h >= mid
            held = bar.c <= mid
        run = run + 1 if held else 0
        if run >= ABSORPTION_BARS and pressed:
            return Signal("ABSORPTION", f"{ABSORPTION_BARS} qirinj e mbajtën gjysmën e mirë të zonës")
    return None


def holds_for(
    setup: Setup, ctx, bars: list[Candle], scale: Scale, extreme: float, price: float
) -> list[str]:
    holds: list[str] = []
    if not bars:
        holds.append("FRESH")
    if (
        not ctx.data_ok
        or ctx.bid is None
        or ctx.quote_synthetic
        or ctx.quote_age > QUOTE_MAX_AGE_S
        or not scale.usable
    ):
        holds.append("DATA")
    spread = ctx.spread
    if spread is not None:
        cap = max(SPREAD_SPIKE_MULT * ctx.median_spread(), SPREAD_SPIKE_ATR * scale.atr)
        if cap > 0 and spread > cap:
            holds.append("SPREAD")
    if scale.usable and len(bars) >= KNIFE_BARS:
        recent = bars[-KNIFE_BARS:]
        adverse = (recent[0].o - recent[-1].c) if setup.is_long else (recent[-1].c - recent[0].o)
        if adverse > KNIFE_ATR * scale.atr:
            holds.append("KNIFE")
    if bars and scale.usable and reaction_of(setup, extreme, price) < MIN_REACTION_ATR * scale.atr:
        holds.append("REACTION")
    return holds


def reaction_of(setup: Setup, extreme: float, price: float) -> float:
    """How far price has come off the worst point since the touch."""
    return (price - extreme) if setup.is_long else (extreme - price)


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


def zone_failed(setup: Setup, ctx, touch_ts: float) -> bool:
    """Consecutive closes clearly beyond the far edge: the zone is being broken, not defended.

    This is not a cancellation — only the stop and TP1 cancel. It means the evidence gathered so far
    describes a reaction that no longer exists, so it must not be carried forward.
    """
    bars = window(ctx, touch_ts)
    atr = ctx.atr("M1") or 0.0
    if not atr or len(bars) < FAILURE_BARS:
        return False
    margin = max(FAILURE_ATR * atr, ctx.median_spread())
    tail = bars[-FAILURE_BARS:]
    if setup.is_long:
        return all(bar.c <= setup.zone_low - margin for bar in tail)
    return all(bar.c >= setup.zone_high + margin for bar in tail)


def evaluate(setup: Setup, ctx, touch_ts: float) -> Verdict:
    """The whole verdict for one moment: what the market has shown since the zone was touched."""
    bars = window(ctx, touch_ts)
    scale = Scale(atr=ctx.atr("M1") or 0.0, tol=max(ctx.median_spread(), ctx.sym.tick))
    price = ctx.bid if ctx.bid is not None else setup.zone_mid
    extreme = extreme_since(setup, bars, setup.zone_low if setup.is_long else setup.zone_high)
    signals: list[Signal] = []
    if scale.usable:
        series = ctx.candles("M1")[-AO_MIN_BARS:]
        signals = [
            signal
            for signal in (
                _zone_break(setup, ctx, scale),
                _ao_divergence(setup, series, scale),
                _quasimodo(setup, series, scale),
                _reclaim(setup, bars, scale),
                _rejection(setup, bars, scale),
                _momentum(setup, bars, scale),
                _absorption(setup, bars, scale),
            )
            if signal is not None
        ]
    holds = holds_for(setup, ctx, bars, scale, extreme, price)
    return Verdict(
        signals=signals,
        holds=holds,
        extreme=extreme,
        advance_r=advance_r(setup, price, setup.risk),
        bars=len(bars),
        reaction_atr=reaction_of(setup, extreme, price) / scale.atr if scale.usable else 0.0,
    )
