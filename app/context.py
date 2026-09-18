"""The market context the validator reads: quotes, candles, ATR, spread and session levels."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Profile, SymbolSettings
from app.market import Candle, CandleStore, median_spread


@dataclass
class MarketContext:
    symbol: str
    now_ts: float
    profile: Profile
    sym: SymbolSettings
    store: CandleStore
    bid: float | None = None
    ask: float | None = None
    quote_ts: float | None = None
    quote_synthetic: bool = False  # True when the price came from a candle close, not a tick
    spread_samples: list[float] = field(default_factory=list)
    levels: dict[str, float | None] = field(default_factory=dict)
    paused: bool = False
    data_ok: bool = True

    # ------------------------------------------------------------------ quote
    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def quote_age(self) -> float:
        if self.quote_ts is None:
            return float("inf")
        return max(0.0, self.now_ts - self.quote_ts)

    # -------------------------------------------------------------- measures
    def atr(self, timeframe: str) -> float | None:
        return self.store.atr(self.symbol, timeframe)

    def median_spread(self) -> float:
        return median_spread(self.spread_samples, self.sym.max_spread_abs)

    def candles(self, timeframe: str) -> list[Candle]:
        return self.store.series(self.symbol, timeframe)

    def last_closed(self, timeframe: str) -> Candle | None:
        return self.store.last(self.symbol, timeframe)

    def has_candles(self, timeframe: str, minimum: int = 2) -> bool:
        return len(self.candles(timeframe)) >= minimum

    def level(self, name: str) -> float | None:
        return self.levels.get(name)
