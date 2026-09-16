"""Synthetic market scenarios: explicit M5/M15 bars on a flat background, M1 derived from them.

The context provider only ever exposes candles that are closed at the requested `now`, so rules can
never see the future (which is what makes the golden scenarios meaningful).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import STRICT, SYMBOL_DEFAULTS, TIMEFRAMES, Profile, Settings, load_settings
from app.context import MarketContext
from app.market import Candle, CandleStore, session_levels
from app.timeutil import parse_ny
from tests.helpers import flat_series


def ts(text: str) -> float:
    return parse_ny(text).timestamp()


def derive_m1(bar: Candle) -> list[Candle]:
    """Five consistent M1 bars that visit the M5 bar's high and low and end at its close."""
    o, h, low, c = bar.o, bar.h, bar.l, bar.c
    t = bar.t
    return [
        Candle(t, o, o, o, o),
        Candle(t + 60, o, h, min(o, h), h),
        Candle(t + 120, h, h, low, low),
        Candle(t + 180, low, max(low, c), low, c),
        Candle(t + 240, c, c, c, c),
    ]


@dataclass
class Scenario:
    symbol: str = "XAUUSD"
    price: float = 5650.0
    profile: Profile = STRICT
    background_bars: int = 600  # ~2 days of M5
    spread_sample: float = 0.2  # the 60-minute median the live quote is compared against
    bars: list[Candle] = field(default_factory=list)
    anchor: float = 0.0
    quotes: dict[float, tuple[float, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.store_master = CandleStore(close_grace_s=0.0)

    # -------------------------------------------------------------- building
    def start(self, first_bar_ny: str) -> Scenario:
        """Fill the background so that the first explicit M5 bar opens at `first_bar_ny`."""
        self.anchor = ts(first_bar_ny)
        background = flat_series("M5", self.background_bars, self.anchor, self.price, 1.0)
        self.bars = list(background)
        return self

    def m5(self, o: float, h: float, low: float, c: float, at: str | None = None) -> Scenario:
        open_ts = ts(at) if at else (self.bars[-1].t + 300 if self.bars else self.anchor)
        self.bars.append(Candle(open_ts, o, h, low, c))
        return self

    def sweep(self, at: str, low: float) -> Scenario:
        """A single dip bar that prints a strict swing low (the SSL raid)."""
        return self.m5(self.price, self.price + 0.5, low, self.price + 0.2, at=at)

    def quote(self, at: str, bid: float, ask: float | None = None) -> Scenario:
        self.quotes[ts(at)] = (bid, ask if ask is not None else bid + 0.2)
        return self

    # --------------------------------------------------------------- reading
    def _master(self) -> dict[str, list[Candle]]:
        bars = sorted({c.t: c for c in self.bars}.values(), key=lambda c: c.t)
        end = bars[-1].t + 300
        m1: list[Candle] = []
        for bar in bars[-400:]:
            m1.extend(derive_m1(bar))
        return {
            "M5": bars,
            "M1": m1,
            "M15": flat_series("M15", 200, end, self.price, 2.0),
            "M30": flat_series("M30", 100, end, self.price, 3.0),
            "H1": flat_series("H1", 100, end, self.price, 5.0),
            "H4": flat_series("H4", 60, end, self.price, 10.0),
            "D1": flat_series("D1", 60, end, self.price, 20.0),
            "W1": flat_series("W1", 12, end, self.price, 60.0),
        }

    def quote_at(self, now: float) -> tuple[float | None, float | None]:
        best: tuple[float, float] | None = None
        best_ts = -1.0
        for stamp, value in self.quotes.items():
            if stamp <= now and stamp > best_ts:
                best, best_ts = value, stamp
        if best is not None:
            return best
        closed = [c for c in self.bars if c.t + 300 <= now]
        if not closed:
            return None, None
        return closed[-1].c, closed[-1].c + 0.2

    def context_provider(self):
        master = self._master()

        def provider(symbol: str, now: float) -> MarketContext:
            store = CandleStore(close_grace_s=0.0)
            for timeframe, series in master.items():
                tf = TIMEFRAMES[timeframe]
                visible = [c for c in series if c.t + tf <= now]
                if visible:
                    store.merge(symbol, timeframe, visible, now)
            bid, ask = self.quote_at(now)
            ctx = MarketContext(
                symbol=symbol,
                now_ts=now,
                profile=self.profile,
                sym=SYMBOL_DEFAULTS.get(symbol, SYMBOL_DEFAULTS["XAUUSD"]),
                store=store,
                bid=bid,
                ask=ask,
                quote_ts=now,
                spread_samples=[self.spread_sample] * 60,
            )
            ctx.levels = session_levels(store, symbol, now)
            return ctx

        return provider


def settings_for(profile_name: str = "STRICT", **extra: str) -> Settings:
    env = {"VALIDATOR_PROFILE": profile_name, "TELEGRAM_CHAT_ID": "42", "TELEGRAM_BOT_TOKEN": "t", **extra}
    return load_settings(env)
