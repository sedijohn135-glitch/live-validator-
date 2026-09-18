"""Synthetic market builders shared by the unit and scenario tests."""

from __future__ import annotations

from app.config import SYMBOL_DEFAULTS, TIMEFRAMES, UNIVERSAL, Profile
from app.context import MarketContext
from app.market import Candle, CandleStore, session_levels
from app.timeutil import NY, ny_datetime, parse_ny


def ts(text: str) -> float:
    """Epoch seconds for a New York wall-clock time written as 'YYYY-MM-DD HH:MM'."""
    return parse_ny(text).timestamp()


def day_start(text: str) -> float:
    return ny_datetime(parse_ny(text).astimezone(NY).date(), 0).timestamp()


def flat_series(timeframe: str, count: int, end_ts: float, price: float, spread: float = 1.0) -> list[Candle]:
    """`count` closed candles ending with the bar that closes at `end_ts`, oscillating around `price`."""
    tf = TIMEFRAMES[timeframe]
    candles: list[Candle] = []
    for i in range(count):
        open_ts = end_ts - (count - i) * tf
        drift = (i % 3 - 1) * spread * 0.2
        o = price + drift
        c = price + drift * 0.5
        candles.append(Candle(open_ts, o, max(o, c) + spread, min(o, c) - spread, c))
    return candles


def candle(timeframe: str, open_ts: float, o: float, h: float, low: float, c: float) -> Candle:
    del timeframe
    return Candle(open_ts, o, h, low, c)


class MarketBuilder:
    """Builds a `CandleStore` plus a `MarketContext` for rule tests."""

    def __init__(
        self,
        symbol: str = "XAUUSD",
        now_ny: str = "2026-09-16 08:10",
        price: float = 5650.0,
        profile: Profile = UNIVERSAL,
    ) -> None:
        self.symbol = symbol
        self.profile = profile
        self.now_ts = ts(now_ny)
        self.price = price
        self.store = CandleStore(close_grace_s=0.0)
        self.fill_defaults()

    def fill_defaults(self, spread: float = 1.0) -> None:
        for tf, count in (("M1", 300), ("M5", 200), ("M15", 120), ("M30", 60), ("H1", 80), ("H4", 40), ("D1", 60)):
            self.store.merge(self.symbol, tf, flat_series(tf, count, self.now_ts, self.price, spread), self.now_ts)
        self.store.merge(
            self.symbol, "W1", flat_series("W1", 10, self.now_ts, self.price, spread * 5), self.now_ts
        )

    def put(self, timeframe: str, candles: list[Candle]) -> None:
        self.store.merge(self.symbol, timeframe, candles, self.now_ts + 10**6)

    def replace_tail(self, timeframe: str, candles: list[Candle]) -> None:
        """Overwrite the newest bars of a series (candles must line up with the grid)."""
        self.put(timeframe, candles)

    def context(self, bid: float | None = None, ask: float | None = None, **kwargs) -> MarketContext:
        bid = self.price if bid is None else bid
        ask = bid + 0.2 if ask is None else ask
        ctx = MarketContext(
            symbol=self.symbol,
            now_ts=self.now_ts,
            profile=self.profile,
            sym=SYMBOL_DEFAULTS[self.symbol],
            store=self.store,
            bid=bid,
            ask=ask,
            quote_ts=self.now_ts,
            spread_samples=[ask - bid] * 60,
            **kwargs,
        )
        ctx.levels = session_levels(self.store, self.symbol, self.now_ts)
        return ctx


def grid(timeframe: str, end_ts: float, count: int) -> list[float]:
    """Open timestamps of the last `count` bars that close at or before `end_ts`."""
    tf = TIMEFRAMES[timeframe]
    return [end_ts - (count - i) * tf for i in range(count)]
