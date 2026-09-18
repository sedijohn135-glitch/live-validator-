"""A synthetic market and a driveable engine, so a whole setup lifecycle fits in one test."""

from __future__ import annotations

from app.config import SYMBOL_DEFAULTS, TIMEFRAMES, UNIVERSAL, load_settings
from app.context import MarketContext
from app.engine import Engine
from app.market import Candle, CandleStore, session_levels
from app.store import Store
from app.timeutil import parse_ny

M1 = 60


def ts(text: str) -> float:
    """Epoch seconds for a New York wall clock written as 'YYYY-MM-DD HH:MM'."""
    return parse_ny(text).timestamp()


class Tape:
    """A tape of M1 candles. Higher timeframes are aggregated from it, never invented."""

    def __init__(self, symbol: str = "XAUUSD", start_ny: str = "2026-09-18 09:00", price: float = 4300.0) -> None:
        self.symbol = symbol
        self.start = ts(start_ny)
        self.price = price
        self.bars: list[Candle] = []

    @property
    def now(self) -> float:
        return self.start + len(self.bars) * M1

    def push(self, o: float, h: float, low: float, c: float) -> Candle:
        bar = Candle(self.now, o, h, low, c)
        self.bars.append(bar)
        self.price = c
        return bar

    def drift(self, count: int, step: float = 0.0, span: float = 0.4) -> None:
        """`count` ordinary candles walking by `step` each bar, with `span` of noise."""
        for _ in range(count):
            o = self.price
            c = o + step
            self.push(o, max(o, c) + span, min(o, c) - span, c)

    def sweep(self, low: float, close: float, span: float = 0.2) -> None:
        """One candle that trades down to `low` and closes back at `close` (mirror for shorts)."""
        o = self.price
        self.push(o, max(o, close) + span, min(low, o, close), close)

    def spike(self, high: float, close: float, span: float = 0.2) -> None:
        o = self.price
        self.push(o, max(high, o, close), min(o, close) - span, close)

    def store(self) -> CandleStore:
        store = CandleStore(close_grace_s=0.0)
        store.merge(self.symbol, "M1", list(self.bars), self.now + 10**6)
        for timeframe in ("M5", "M15", "M30", "H1", "H4", "D1", "W1"):
            store.merge(self.symbol, timeframe, self._aggregate(TIMEFRAMES[timeframe]), self.now + 10**6)
        return store

    def _aggregate(self, seconds: int) -> list[Candle]:
        buckets: dict[float, list[Candle]] = {}
        for bar in self.bars:
            buckets.setdefault(bar.t - (bar.t % seconds), []).append(bar)
        out = []
        for open_ts, group in sorted(buckets.items()):
            out.append(
                Candle(
                    open_ts,
                    group[0].o,
                    max(b.h for b in group),
                    min(b.l for b in group),
                    group[-1].c,
                )
            )
        return out

    def context(
        self, bid: float | None = None, spread: float = 0.2, median: float | None = None, **kwargs
    ) -> MarketContext:
        """`median` is the usual spread; `spread` is the one right now, so a spike can be simulated."""
        bid = self.price if bid is None else bid
        store = self.store()
        ctx = MarketContext(
            symbol=self.symbol,
            now_ts=kwargs.pop("now_ts", self.now),
            profile=UNIVERSAL,
            sym=SYMBOL_DEFAULTS[self.symbol],
            store=store,
            bid=bid,
            ask=bid + spread,
            quote_ts=kwargs.pop("quote_ts", self.now),
            spread_samples=[spread if median is None else median] * 60,
            **kwargs,
        )
        ctx.levels = session_levels(store, self.symbol, ctx.now_ts)
        return ctx


class Feed:
    """Engine plus tape: push candles, set the price, let the engine decide."""

    def __init__(self, tmp_path, symbol: str = "XAUUSD", start_ny: str = "2026-09-18 09:00", price: float = 4300.0):
        self.settings = load_settings(
            {
                "PUBLIC_BASE_URL": "https://validator.test",
                "OWNER_PASSWORD": "a-very-long-password",
                "TELEGRAM_BOT_TOKEN": "fake",
                "TELEGRAM_CHAT_ID": "42",
                "DATA_DIR": str(tmp_path),
            }
        )
        self.store = Store(str(tmp_path / "validator.db"))
        self.tape = Tape(symbol, start_ny, price)
        self.bid = price
        self.spread = 0.2
        self.data_ok = True
        self.engine = Engine(self.settings, self.store, self.context, clock=lambda: self.tape.now)

    def context(self, symbol: str, now: float) -> MarketContext:
        return self.tape.context(bid=self.bid, spread=self.spread, now_ts=now, quote_ts=now, data_ok=self.data_ok)

    def submit(self, **payload) -> dict:
        payload.setdefault("symbol", self.tape.symbol)
        return self.engine.submit(payload)

    def tick(self, price: float | None = None) -> None:
        if price is not None:
            self.bid = price
        self.engine.process_symbol(self.tape.symbol, self.tape.now)

    def messages(self) -> list[str]:
        return [row["text"] for row in self.store.pending_messages(200)]

    def last_message(self) -> str:
        messages = self.messages()
        return messages[-1] if messages else ""

    def state(self, setup_id: str) -> str:
        row = self.store.get_setup(setup_id)
        return "" if row is None else row["state"]

    def outcome(self, setup_id: str) -> str:
        row = self.store.get_setup(setup_id)
        return "" if row is None else (row["outcome"] or "")


class TapeRuntime:
    """The real `Runtime`, with its market data coming from a tape instead of the network.

    Built as a factory rather than a subclass so the MCP tools see the genuine runtime object.
    """

    @staticmethod
    def build(tmp_path, tape: Tape, **env):
        from collections import deque

        from app.ctrader import Quote
        from app.runtime import Runtime
        from tests.app_harness import make_runtime

        base, _fake, telegram = make_runtime(tmp_path, **env)
        runtime = Runtime(
            base.settings,
            Store(str(tmp_path / "tools.db")),
            ctrader=base.ctrader,
            telegram=telegram.client(),
            clock=lambda: tape.now,
        )

        def load(*_args, **_kwargs):
            runtime.candles = tape.store()
            runtime.quotes[tape.symbol] = Quote(tape.symbol, tape.price, tape.price + 0.2, tape.now)
            samples = runtime.spread_samples.setdefault(tape.symbol, deque(maxlen=1800))
            samples.extend([0.2] * 60)
            runtime.last_quote_at = tape.now
            runtime.data_status = "ok"

        async def aload(*args, **kwargs):
            load()

        load()
        runtime.ensure_history = aload
        runtime.refresh_candles = aload
        runtime.refresh_quotes = aload
        runtime.refresh_quotes_for = aload
        runtime.prepare_for_submit = aload
        return runtime, telegram
