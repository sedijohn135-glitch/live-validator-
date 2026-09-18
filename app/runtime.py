"""Runtime glue: market polling, the engine tick, Telegram, the lease and `/health`.

All background tasks start once from the MCP server lifespan and only run while this instance holds
the engine lease, so a deploy overlap can never send a message twice (architecture §5).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from app import telegram as tg
from app.config import TIMEFRAMES, VERSION, Settings, load_settings
from app.context import MarketContext
from app.ctrader import AuthError, CTraderClient, DataError, Quote
from app.engine import OPEN_STATES, Engine
from app.evidence import SCORE_MIN
from app.market import CandleStore, build_snapshot, median_spread, session_levels
from app.store import Store
from app.timeutil import (
    active_macro,
    active_windows,
    from_epoch,
    in_lunch,
    is_market_open,
    next_windows,
    ny_string,
    to_ny,
    to_utc,
)

logger = logging.getLogger(__name__)

SNAPSHOT_TIMEFRAMES = ("D1", "H4", "H1", "M15", "M5", "M1")
# What /selftest and the data block print, and the subset a snapshot cannot do without.
REPORT_TIMEFRAMES = ("M1", "M5", "M15", "H1", "H4", "D1")
REQUIRED_TIMEFRAMES = ("M1", "M5", "M15", "H1", "D1")
HISTORY_COUNTS = {"W1": 12, "D1": 300, "H4": 120, "H1": 300, "M30": 120, "M15": 300, "M5": 400, "M1": 1500}
SNAPSHOT_CACHE_S = 15.0
QUOTE_FALLBACK_AFTER_S = 8.0  # beyond this the newest candle close stands in for the tick
LEASE_TTL_S = 30.0
LEASE_HEARTBEAT_S = 10.0
IDLE_HEARTBEAT_S = 300.0
OUTAGE_PAUSE_S = 20.0
RECONNECT_EVERY_S = 120.0  # while the feed is down, rebuild the link this often


class SecretFilter(logging.Filter):
    """Never let a token or password reach the logs (failure mode T7)."""

    def __init__(self, secrets_provider: Callable[[], list[str]]) -> None:
        super().__init__()
        self._provider = secrets_provider

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        for value in self._provider():
            if value and len(value) >= 8 and value in message:
                message = message.replace(value, "***")
                record.msg = message
                record.args = ()
        return True


class Runtime:
    """Owns the live market state and every background task."""

    def __init__(
        self,
        settings: Settings | None = None,
        store: Store | None = None,
        ctrader: CTraderClient | None = None,
        telegram: tg.TelegramClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings or load_settings()
        self.store = store or Store(self.settings.db_path)
        self.clock = clock
        self.candles = CandleStore(close_grace_s=self.settings.profile.close_grace_s)
        self.quotes: dict[str, Quote] = {}
        self.spread_samples: dict[str, deque] = {s: deque(maxlen=1800) for s in self.settings.symbols}
        self.ctrader = ctrader or CTraderClient(self.settings, self.store)
        self.telegram = telegram
        self.engine = Engine(self.settings, self.store, self.make_context, clock=self.clock)
        self.holder = secrets.token_hex(8)
        self.has_lease = False
        self.data_status = "not_configured"
        self.data_error = ""
        self.last_quote_at = 0.0
        self.last_snapshot: dict[str, tuple[float, dict]] = {}
        self.started_at = self.clock()
        self._last_outage_alert = 0.0
        self._last_auth_alert = 0.0
        self._last_forced_reconnect = 0.0
        self._telegram_offset = 0
        self._tasks: list[asyncio.Task] = []
        self._history_loaded: set[tuple[str, str]] = set()
        self.start_count = 0  # asserted by the "lifespan runs once" test

    # --------------------------------------------------------------- context
    def quote_for(self, symbol: str, now: float) -> Quote | None:
        """The live tick, or the newest candle close standing in for it while the feed hiccups.

        A synthetic quote is enough to arm a setup and to judge invalidation, but T-07 refuses it,
        so a candle close can never become an ENTER.
        """
        quote = self.quotes.get(symbol)
        if quote is not None and now - quote.ts <= QUOTE_FALLBACK_AFTER_S:
            return quote
        fallback = self._candle_quote(symbol, now)
        if fallback is None:
            return quote
        if quote is not None and quote.ts >= fallback.ts:
            return quote
        return fallback

    def _candle_quote(self, symbol: str, now: float) -> Quote | None:
        for timeframe in ("M1", "M5"):
            candle = self.candles.last(symbol, timeframe)
            if candle is None:
                continue
            spread = median_spread(
                list(self.spread_samples.get(symbol, ())), self.settings.symbol(symbol).max_spread_abs
            )
            close_ts = candle.t + TIMEFRAMES[timeframe]
            return Quote(symbol, candle.c, candle.c + spread, min(close_ts, now), synthetic=True)
        return None

    def make_context(self, symbol: str, now: float | None = None) -> MarketContext:
        now = self.clock() if now is None else now
        quote = self.quote_for(symbol, now)
        profile = self.settings.profile
        ctx = MarketContext(
            symbol=symbol,
            now_ts=now,
            profile=profile,
            sym=self.settings.symbol(symbol),
            store=self.candles,
            bid=quote.bid if quote else None,
            ask=quote.ask if quote else None,
            quote_ts=quote.ts if quote else None,
            quote_synthetic=bool(quote and quote.synthetic),
            spread_samples=list(self.spread_samples.get(symbol, ())),
            paused=self.paused,
            data_ok=self.data_status == "ok",
        )
        ctx.levels = session_levels(self.candles, symbol, now)
        return ctx

    @property
    def paused(self) -> bool:
        """No data, no triggers. `/pause` and every unhealthy data state stop ENTER messages."""
        return self.engine.paused() or self.data_status in ("down", "auth_error", "not_configured")

    # ----------------------------------------------------------------- notify
    def notify(self, key: str, text: str) -> None:
        """Queue a system message. `key` keeps repeats out of the outbox."""
        if not self.settings.telegram_chat_id:
            return
        body = getattr(tg, text, text)
        self.store.queue_message(f"sys:{key}", self.settings.telegram_chat_id, body, self.clock())

    # ------------------------------------------------------------ market data
    async def refresh_quotes(self) -> None:
        symbols = self._symbols_to_watch()
        if not symbols:
            return
        try:
            quotes = await self.ctrader.quotes(symbols)
        except AuthError as exc:
            self._on_auth_error(exc)
            return
        except DataError as exc:
            self._on_data_error(exc)
            return
        now = self.clock()
        for symbol, quote in quotes.items():
            self.quotes[symbol] = quote
            self.spread_samples.setdefault(symbol, deque(maxlen=1800)).append(quote.spread)
        if quotes:
            self.last_quote_at = now
            self._on_data_ok()

    def _symbols_to_watch(self) -> list[str]:
        active = {row["symbol"] for row in self.store.setups_in_state(OPEN_STATES)}
        recent = {s for s, (stamp, _payload) in self.last_snapshot.items() if self.clock() - stamp < 300}
        return [s for s in self.settings.symbols if s in active or s in recent]

    async def refresh_candles(self, symbol: str, timeframes: tuple[str, ...], count: int = 3) -> bool:
        """Fetch candles. Returns False when nothing arrived, so no caller can mistake it for health."""
        now = self.clock()
        fetched = False
        for timeframe in timeframes:
            try:
                bars = await self.ctrader.candles(symbol, timeframe, count=count, end_ts=now)
            except AuthError as exc:
                self._on_auth_error(exc)
                return False
            except DataError as exc:
                self._on_data_error(exc)
                return False
            if bars:
                keep = HISTORY_COUNTS.get(timeframe, 400)
                self.candles.merge(symbol, timeframe, bars, now, keep=keep)
                fetched = True
        if fetched:
            self._on_data_ok()
        return fetched

    async def ensure_history(self, symbol: str, timeframes: tuple[str, ...] | None = None) -> None:
        """Fetch the deep history a snapshot or a newly armed setup needs."""
        wanted = timeframes or tuple(HISTORY_COUNTS)
        for timeframe in wanted:
            if (symbol, timeframe) in self._history_loaded:
                continue
            # Only remember a timeframe once it really arrived: a failed fetch must be retried,
            # otherwise a token that is fixed later never refills the history.
            if await self.refresh_candles(symbol, (timeframe,), count=HISTORY_COUNTS.get(timeframe, 300)):
                self._history_loaded.add((symbol, timeframe))

    def due_timeframes(self, symbol: str, now: float) -> tuple[str, ...]:
        """Timeframes whose newest bar should have closed by now."""
        # The universal validator always reads the same timeframes: M1 carries the evidence, the
        # rest carry the structure the secure level is built from. No setup asks for more.
        needed = set(REQUIRED_TIMEFRAMES)
        due = []
        grace = self.settings.profile.close_grace_s
        for timeframe in needed:
            seconds = TIMEFRAMES[timeframe]
            last = self.candles.last(symbol, timeframe)
            expected_open = ((now - grace) // seconds) * seconds - seconds
            if last is None or last.t < expected_open:
                due.append(timeframe)
        return tuple(due)

    # ---------------------------------------------------------- data status
    def _on_auth_error(self, exc: Exception) -> None:
        # A fixed dedupe key would alert once for the lifetime of the database: the second time the
        # token dies, weeks later, the owner would hear nothing. Repeat hourly while it lasts.
        now = self.clock()
        self.data_status = "auth_error"
        self.data_error = str(exc)[:200]
        if now - self._last_auth_alert < 3600.0:
            return
        self._last_auth_alert = now
        self.notify(f"auth_expired:{int(now)}", "AUTH_EXPIRED")

    def _on_data_error(self, exc: Exception) -> None:
        now = self.clock()
        self.data_status = "down"
        self.data_error = str(exc)[:200]
        market_open = any(is_market_open(s, from_epoch(now)) for s in self.settings.symbols)
        if not market_open:
            return
        if now - self._last_outage_alert >= max(3600.0, self.settings.profile.data_outage_alert_s):
            self._last_outage_alert = now
            self.store.queue_message(
                f"sys:data_down:{int(now // 3600)}",
                self.settings.telegram_chat_id,
                tg.DATA_DOWN.format(reason=tg.esc(self.data_error or "pa përgjigje")),
                now,
            )

    async def _heal_connection(self, now: float) -> None:
        """While the feed is down, force a fresh connection every two minutes.

        `call()` already reconnects on an error it recognises as transient. This is the net under
        that: whatever wording a broken link arrives in, the link is rebuilt instead of the engine
        sitting on it for hours. An expired token is excluded — a reconnect cannot fix that.
        """
        if self.data_status != "down" or now - self._last_forced_reconnect < RECONNECT_EVERY_S:
            return
        self._last_forced_reconnect = now
        with contextlib.suppress(Exception):
            await self.ctrader.reconnect()

    def _on_data_ok(self) -> None:
        was_down = self.data_status in ("down", "auth_error")
        if self.data_status == "auth_error":
            self._last_auth_alert = 0.0  # a token that dies again right after a fix must alert again
        self.data_status = "ok"
        self.data_error = ""
        if was_down:
            self.store.queue_message(
                f"sys:restored:{int(self.clock())}",
                self.settings.telegram_chat_id,
                tg.RESTORED.format(summary="setup-et u rikontrolluan"),
                self.clock(),
            )

    # ------------------------------------------------------------------ ticks
    async def tick(self) -> None:
        """One engine pass: quotes, due candles, then the state machine per symbol."""
        now = self.clock()
        await self._heal_connection(now)
        await self.refresh_quotes()
        for symbol in self.settings.symbols:
            if not self._is_watched(symbol):
                continue
            due = self.due_timeframes(symbol, now)
            if due:
                await self.refresh_candles(symbol, due, count=3)
        if self.data_status != "ok" and self.last_quote_at and now - self.last_quote_at > OUTAGE_PAUSE_S:
            return  # PAUSED: no triggers without data
        for symbol in self.settings.symbols:
            if self._is_watched(symbol):
                self.engine.process_symbol(symbol, self.clock())

    def _is_watched(self, symbol: str) -> bool:
        return symbol in self._symbols_to_watch()

    # -------------------------------------------------------------- snapshot
    async def snapshot(self, symbol: str) -> dict[str, Any]:
        now = self.clock()
        cached = self.last_snapshot.get(symbol)
        if cached and now - cached[0] < SNAPSHOT_CACHE_S:
            return cached[1]
        await self.ensure_history(symbol)
        await self.refresh_candles(symbol, SNAPSHOT_TIMEFRAMES, count=3)
        await self.refresh_quotes_for(symbol)
        decimals = self.settings.symbol(symbol).display_decimals
        quote = self.quote_for(symbol, now)
        payload = build_snapshot(
            symbol,
            self.candles,
            now,
            decimals,
            {
                "bid": round(quote.bid, decimals),
                "ask": round(quote.ask, decimals),
                "spread": round(quote.spread, decimals),
                "age_s": int(max(0, now - quote.ts)),
            }
            if quote
            else None,
            session_levels(self.candles, symbol, now),
            self.time_block(symbol, now),
            self.data_block(symbol, quote is not None, now),
        )
        if payload["data"]["usable"]:
            self.last_snapshot[symbol] = (now, payload)
        return payload

    def data_block(self, symbol: str, has_quote: bool, now: float | None = None) -> dict[str, Any]:
        """Whether this snapshot can be analysed at all, and why not when it cannot."""
        now = self.clock() if now is None else now
        # H4 is reported but not gated on: the trigger loop never reads it, so a thin H4 history
        # must not make an otherwise complete snapshot unusable.
        bars = {tf: len(self.candles.series(symbol, tf)) for tf in REPORT_TIMEFRAMES}
        usable = has_quote and all(bars[tf] >= 15 for tf in REQUIRED_TIMEFRAMES)
        block: dict[str, Any] = {
            "status": self.data_status,
            "usable": usable,
            "market_open": is_market_open(symbol, from_epoch(now)),
            "symbols_resolved": sorted(self.ctrader.symbols),
            "candles_held": bars,
        }
        # The reason belongs in the answer whenever the feed is unhealthy, not only when it is
        # unusable: a snapshot that still works off cached bars must still say what broke.
        if not usable or self.data_status != "ok":
            block["detail"] = self.data_error or (
                "cTrader nuk po kthen të dhëna — kontrollo CTRADER_MCP_CONFIG dhe dërgo /selftest"
            )
            block["warnings"] = (self.settings.warnings + self.ctrader.warnings)[:5]
        return block

    async def prepare_for_submit(self, symbol: str) -> None:
        """Fresh data before the intake gate runs: G-02 must never arm on stale or missing candles."""
        await self.ensure_history(symbol)
        await self.refresh_candles(symbol, SNAPSHOT_TIMEFRAMES, count=3)
        await self.refresh_quotes_for(symbol)

    async def refresh_quotes_for(self, symbol: str) -> None:
        try:
            quotes = await self.ctrader.quotes([symbol])
        except AuthError as exc:
            self._on_auth_error(exc)
            return
        except DataError as exc:
            self._on_data_error(exc)
            return
        for name, quote in quotes.items():
            self.quotes[name] = quote
            self.spread_samples.setdefault(name, deque(maxlen=1800)).append(quote.spread)
        if quotes:
            self.last_quote_at = self.clock()
            self._on_data_ok()

    def time_block(self, symbol: str, now: float) -> dict[str, Any]:
        moment = from_epoch(now)
        ny = to_ny(moment)
        return {
            "utc": to_utc(moment).strftime("%Y-%m-%d %H:%M"),
            "ny": ny.strftime("%Y-%m-%d %H:%M"),
            "weekday_ny": ny.strftime("%A"),
            "market_open": is_market_open(symbol, moment),
            "active_windows": active_windows(moment),
            "active_macro": active_macro(moment),
            "next_windows": [list(pair) for pair in next_windows(moment)],
            "lunch_block": in_lunch(moment),
        }

    # ---------------------------------------------------------------- health
    def health(self) -> dict[str, Any]:
        now = self.clock()
        active = self.store.setups_in_state(OPEN_STATES)
        return {
            "ok": True,
            "version": VERSION,
            "profile": self.settings.profile.name,
            "uptime_s": int(now - self.started_at),
            "data": {
                "ctrader": self.data_status,
                "last_quote_age_s": int(now - self.last_quote_at) if self.last_quote_at else None,
                "symbols": sorted(self.ctrader.symbols),
                "account": self.ctrader.credentials.account if self.ctrader.credentials else "",
                "detail": self.data_error if self.data_status != "ok" else "",
            },
            "telegram": "ok" if self.settings.telegram_configured() else "not_configured",
            "mcp_auth": "open" if self.settings.mcp_open else "oauth",
            "volume": "ok" if self.settings.on_volume else "missing",
            "active_setups": len(active),
            "paused": self.paused,
            "warnings": self.settings.warnings + self.ctrader.warnings,
        }

    # ----------------------------------------------------------- background
    async def run_forever(self) -> None:
        """Start every background task once. Cancelled by the lifespan on shutdown."""
        self.start_count += 1
        self._tasks = [
            asyncio.create_task(self._lease_loop(), name="lease"),
            asyncio.create_task(self._engine_loop(), name="engine"),
            asyncio.create_task(self._sender_loop(), name="telegram-sender"),
            asyncio.create_task(self._commands_loop(), name="telegram-commands"),
            asyncio.create_task(self._report_loop(), name="daily-report"),
        ]

    async def stop(self, timeout: float = 5.0) -> None:
        """Cancel every task and give up waiting after `timeout`.

        A task cancelled inside a nested MCP client can fail to unwind cleanly, and shutdown must
        never be the thing that hangs a deploy.
        """
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            with contextlib.suppress(Exception):
                await asyncio.wait(self._tasks, timeout=timeout)
        self._tasks = []
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.ctrader.aclose(), timeout=timeout)

    async def _lease_loop(self) -> None:
        while True:
            self.has_lease = self.store.acquire_lease("engine", self.holder, LEASE_TTL_S, self.clock())
            await asyncio.sleep(LEASE_HEARTBEAT_S)

    async def _engine_loop(self) -> None:
        try:
            await self._startup()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("startup failed")
        last_idle = 0.0
        while True:
            try:
                if self.has_lease:
                    if self._symbols_to_watch():
                        await self.tick()
                    elif self.clock() - last_idle > IDLE_HEARTBEAT_S:
                        last_idle = self.clock()
                        await self._heartbeat()
            except Exception:
                logger.exception("engine tick failed")
            await asyncio.sleep(self.settings.profile.quote_poll_s)

    async def _startup(self) -> None:
        if not self.settings.on_volume and self.settings.warnings:
            self.notify("no_volume", "NO_VOLUME")
        if self.ctrader.load_credentials() is None:
            self.data_status = "not_configured"
            return
        try:
            info = await self.ctrader.discover()
        except AuthError as exc:
            self._on_auth_error(exc)
            return
        except Exception as exc:  # noqa: BLE001 - startup must never crash the process
            self._on_data_error(exc)
            return
        if info["profile"] == "trading":
            self.notify("trading_profile", "TRADING_PROFILE")
        self._on_data_ok()
        for symbol in self.settings.symbols:
            # Chunked history is dozens of calls; pay for it here, in the background, not inside the
            # first market_snapshot Gemini asks for.
            with contextlib.suppress(Exception):
                await self.ensure_history(symbol)

    async def _heartbeat(self) -> None:
        if self.ctrader.credentials is None and self.ctrader.load_credentials() is None:
            self.data_status = "not_configured"
            return
        try:
            await self.ctrader.call("get_version")
            self._on_data_ok()
        except AuthError as exc:
            self._on_auth_error(exc)
        except Exception as exc:  # noqa: BLE001
            self._on_data_error(exc)

    async def _sender_loop(self) -> None:
        while True:
            try:
                client = self._telegram_client()
                if client is not None and self.has_lease:
                    sender = tg.OutboxSender(self.store, client, self.settings.telegram_chat_id)
                    await sender.drain_once()
            except Exception:
                logger.exception("telegram sender failed")
            await asyncio.sleep(2.0)

    async def _commands_loop(self) -> None:
        while True:
            try:
                client = self._telegram_client()
                if client is not None and self.has_lease:
                    updates = await client.get_updates(self._telegram_offset, timeout=25)
                    for update in updates:
                        self._telegram_offset = max(self._telegram_offset, int(update.get("update_id", 0)) + 1)
                        await self.handle_update(update, client)
                    if not updates:
                        # A server (or a fake) that answers immediately must not spin the loop.
                        await asyncio.sleep(1.0)
                else:
                    await asyncio.sleep(5.0)
            except Exception:
                logger.exception("telegram command poller failed")
                await asyncio.sleep(5.0)

    async def _report_loop(self) -> None:
        while True:
            now = self.clock()
            ny = to_ny(from_epoch(now))
            if self.has_lease and ny.hour == 17 and ny.minute == 5:
                self.send_daily_report()
                await asyncio.sleep(90)
            await asyncio.sleep(30)

    def send_daily_report(self) -> None:
        stats = self.engine.stats(1)
        date = to_ny(from_epoch(self.clock())).strftime("%Y-%m-%d")
        text = tg.daily_report({**stats, "date": date})
        self.store.queue_message(f"report:{date}", self.settings.telegram_chat_id, text, self.clock())

    def _telegram_client(self) -> tg.TelegramClient | None:
        if self.telegram is not None:
            return self.telegram
        if not self.settings.telegram_bot_token:
            return None
        self.telegram = tg.TelegramClient(self.settings.telegram_bot_token)
        return self.telegram

    # -------------------------------------------------------------- commands
    async def handle_update(self, update: dict[str, Any], client: tg.TelegramClient) -> None:
        message = update.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id") or "")
        text = (message.get("text") or "").strip()
        if not text:
            return
        owner = self.settings.telegram_chat_id
        if not owner:
            if text.startswith("/start"):
                await client.send_message(
                    chat_id,
                    "Chat ID: <b>" + tg.esc(chat_id) + "</b>\nVendose te Railway si TELEGRAM_CHAT_ID.",
                )
            return
        if chat_id != owner:
            return  # strangers are ignored silently
        if text.startswith("/ctrader"):
            message_id = message.get("message_id")
            if message_id:
                await client.delete_message(chat_id, int(message_id))
        reply = await self.run_command(text)
        if reply:
            await client.send_message(owner, reply)

    async def run_command(self, text: str) -> str:
        command, _, argument = text.partition(" ")
        command = command.lower().lstrip("/").split("@")[0]
        argument = argument.strip()
        if command == "start":
            return "✅ Je pronari. Shkruaj /help për komandat."
        if command == "help":
            return tg.HELP_TEXT
        if command == "status":
            return self._status_text()
        if command == "active":
            return self._active_text()
        if command == "cancel":
            result = self.engine.cancel(argument)
            return f"Anulimi: {tg.esc(result['status'])} ({tg.esc(argument)})"
        if command == "pause":
            self.engine.set_paused(True)
            return "⏸️ Pauzë: asnjë mesazh HYR derisa të shkruash /resume."
        if command == "resume":
            self.engine.set_paused(False)
            return "▶️ Monitorimi vazhdoi."
        if command == "stats":
            days = 30 if argument.strip() == "30" else 7
            stats = self.engine.stats(days)
            return tg.daily_report({**stats, "date": f"{days} ditë"})
        if command == "rules":
            return self._rules_text()
        if command == "selftest":
            return await self.selftest()
        if command == "ctrader":
            return await self._ctrader_command(argument)
        if command == "revoke_all":
            from app.oauth import SQLiteOAuthProvider

            provider = SQLiteOAuthProvider(self.settings, self.store)
            count = provider.revoke_all()
            return f"🔌 U shkëputën {count} tokena. Lidhe Gemini-n sërish kur të duash."
        return "Komandë e panjohur. /help"

    async def _ctrader_command(self, argument: str) -> str:
        if argument.lower() == "reset":
            await self.ctrader.swap_credentials(None)
            return "↩️ U kthye te variablat e Railway.\n\n" + await self.selftest()
        if not argument:
            return "Dërgo: /ctrader KONFIGURIMI (ose /ctrader reset)"
        credentials = await self.ctrader.swap_credentials(argument)
        if credentials is None:
            return "❌ Konfigurimi s'u kuptua. Kopjoje sërish nga cTrader Web → Settings → Remote MCP."
        self._history_loaded.clear()
        return f"🔑 Tokeni u rinovua ({tg.esc(credentials.masked)}).\n\n" + await self.selftest()

    def _status_text(self) -> str:
        health = self.health()
        lines = [
            "<b>Gjendja</b>",
            f"cTrader: {tg.esc(health['data']['ctrader'])}"
            + (f" · llogaria {tg.esc(health['data']['account'])}" if health["data"]["account"] else ""),
            f"Çmimi i fundit: {health['data']['last_quote_age_s']} s më parë"
            if health["data"]["last_quote_age_s"] is not None
            else "Çmimi i fundit: -",
            *([f"Arsyeja: {tg.esc(health['data']['detail'])}"] if health["data"]["detail"] else []),
            f"Setup aktive: {health['active_setups']}",
            f"Telegram: {tg.esc(health['telegram'])} · Volume: {tg.esc(health['volume'])}",
            f"Profili: {tg.esc(self.settings.profile.name)} · Uptime: {health['uptime_s']} s",
            f"Pauzë: {'po' if health['paused'] else 'jo'}",
            "Hyrja te /mcp: " + ("⚠️ e hapur (pa fjalëkalim)" if self.settings.mcp_open else "✅ me fjalëkalim"),
        ]
        if health["warnings"]:
            lines.append("⚠️ " + tg.esc("; ".join(health["warnings"][:3])))
        return "\n".join(lines)

    def _active_text(self) -> str:
        overview = self.engine.status()
        if not overview["active"]:
            return "S'ka setup aktive."
        lines = ["<b>Setup aktive</b>"]
        for row in overview["active"]:
            lines.append(
                f"{tg.esc(row['setup_id'])} · {tg.esc(row['symbol'])} {tg.esc(row['direction'])} "
                f"· {tg.esc(row['state'])} · zona {tg.esc(row['zone'][0])}–{tg.esc(row['zone'][1])}"
            )
        return "\n".join(lines)

    def _rules_text(self) -> str:
        """`/rules` explains the only judgement the validator makes."""
        return (
            "<b>Si vendos validatori</b>\n"
            "Asnjë setup nuk refuzohet. Koha, kill zone, premium/discount, modeli: nuk i shoh.\n"
            f"Hyrje kur evidenca live ≥ {SCORE_MIN} pikë dhe ka së paku një sinjal kryesor:\n"
            "• RECLAIM (2) – likuiditeti u mor dhe çmimi u kthye\n"
            "• REJECTION (2) – bisht refuzimi ose qiri gëlltitës te zona\n"
            "• SHIFT (2) – struktura mikro u thye\n"
            "• MOMENTUM (1) – qiri me trup ≥ 0.9 ATR(M1)\n"
            "• ABSORPTION (1) – 3 qirinj pa e humbur zonën\n"
            "Pritje (jo refuzim): spread i lartë, thikë në rënie, të dhëna të vjetra.\n"
            "Nëse çmimi ka ikur > 0.35R → LIMIT me hyrje të rillogaritur.\n"
            "SL rillogaritet: ekstremi i konfirmimit ± max(1.5×ATR(M1), 2×spread, 2 tick).\n"
            "Anulohet vetëm nga: SL para hyrjes, ose TP1 para hyrjes."
        )

    async def selftest(self) -> str:
        """The `/selftest` report of ctrader-remote-mcp §8, one ✅/❌ per line."""
        lines = ["<b>SELFTEST</b>"]
        credentials = self.ctrader.credentials or self.ctrader.load_credentials()
        if credentials is None:
            lines.append("❌ Kredencialet e cTrader mungojnë (CTRADER_MCP_CONFIG ose /ctrader)")
        else:
            account = credentials.account
            lines.append(
                f"✅ Kredencialet: {tg.esc(credentials.source)} · {tg.esc(credentials.masked)}"
                + (f" · llogaria {tg.esc(account)}" if account else "")
            )
            try:
                info = await self.ctrader.discover()
                lines.append(f"✅ get_version: {tg.esc(self.ctrader.version)}")
                lines.append(
                    f"{'⚠️' if info['profile'] == 'trading' else '✅'} Profili: {tg.esc(info['profile'])}"
                )
            except Exception as exc:  # noqa: BLE001 - the report must always render
                lines.append(f"❌ Lidhja: {tg.esc(str(exc)[:120])}")
        for symbol in self.settings.symbols:
            info = self.ctrader.symbols.get(symbol)
            if info is None:
                lines.append(f"❌ {tg.esc(symbol)}: s'u gjet te cTrader")
                continue
            decimals = self.settings.symbol(symbol).display_decimals
            lines.append(
                f"✅ {tg.esc(symbol)}: id {info.symbol_id} · {info.digits} shifra"
                + (" (kalibruar)" if info.calibrated else "")
            )
            quote = self.quotes.get(symbol)
            if quote is None:
                with contextlib.suppress(Exception):
                    await self.refresh_quotes_for(symbol)
                quote = self.quotes.get(symbol)
            if quote is None:
                lines.append(f"❌ {tg.esc(symbol)}: s'ka çmim live")
            else:
                age = int(max(0, self.clock() - quote.ts))
                lines.append(
                    f"✅ {tg.esc(symbol)}: bid {quote.bid:.{decimals}f} / ask {quote.ask:.{decimals}f} "
                    f"· spread {quote.spread:.{decimals}f} · {age}s"
                )
                if age > 5:
                    lines.append(f"⚠️ {tg.esc(symbol)}: ora e serverit ndryshon me {age}s")
            with contextlib.suppress(Exception):
                await self.ensure_history(symbol, REPORT_TIMEFRAMES)
            for timeframe in REPORT_TIMEFRAMES:
                candle = self.candles.last(symbol, timeframe)
                if candle is None:
                    lines.append(f"❌ {tg.esc(symbol)} {timeframe}: s'ka qirinj")
                    continue
                # H4 and D1 open on the broker's 17:00 NY day, not on the epoch grid.
                aligned = candle.t % TIMEFRAMES[timeframe] == 0 if timeframe not in ("H4", "D1") else True
                mark = "✅" if aligned else "⚠️"
                lines.append(f"{mark} {tg.esc(symbol)} {timeframe}: {tg.esc(ny_string(candle.t))} NY")
        lines.append(
            ("✅" if self.settings.telegram_configured() else "❌") + " Telegram i konfiguruar"
        )
        lines.append(("✅" if self.settings.on_volume else "⚠️") + " Volume te Railway")
        lines.append(f"✅ URL publike: {tg.esc(self.settings.public_base_url)}/mcp")
        if self.settings.mcp_open:
            lines.append("⚠️ /mcp është i hapur: kushdo me adresën mund të dërgojë setup (MCP_AUTH=open)")
        else:
            lines.append("✅ /mcp mbrohet me fjalëkalimin e pronarit")
        lines.append(
            "✅ Validator universal: pa filtër kohe, pa refuzime"
        )
        lines.append(f"✅ Profili aktiv: {tg.esc(self.settings.profile.name)}")
        return "\n".join(lines)
