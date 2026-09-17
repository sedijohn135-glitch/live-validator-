"""The validator engine: intake, state machine, triggers, outcomes, shadow tracking and stats.

Every transition writes the state change, its audit event and its outbox row in ONE transaction, so
a restart can never send ENTER twice and never lose one (architecture §6, failure mode E1).
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app import telegram as tg
from app.config import TIMEFRAMES, Settings
from app.context import MarketContext
from app.market import Candle, find_fvgs
from app.rules import (
    IntakeResult,
    TriggerState,
    beyond,
    collect_entry_fvgs,
    compute_score,
    evaluate_intake,
    l01_sl_touch,
    l02_invalidation_close,
    l03_body_respect,
    liquidity_taken,
    t02_cisd,
    t03_displacement,
    t04_freshness,
    t05_time,
    t06_spread,
    t07_price,
    t08_data,
)
from app.setup_model import SetupInput, normalise
from app.store import Store, json_loads
from app.timeutil import NY, friday_close, from_epoch, next_weekday_at, ny_string, parse_ny, to_ny

logger = logging.getLogger(__name__)

ACTIVE_STATES = ("ARMED", "IN_ZONE")
NON_TERMINAL = ("ARMED", "IN_ZONE", "TRIGGERED")
TERMINAL = ("INVALIDATED", "EXPIRED", "MISSED", "CANCELLED", "REPLACED", "REJECTED")

BASE32 = "ABCDEFGHJKMNPQRSTVWXYZ23456789"


def new_setup_id(symbol: str, now_ts: float, rng: random.Random | None = None) -> str:
    rand = rng or random.SystemRandom()
    prefix = symbol.upper()[:3]
    day = to_ny(from_epoch(now_ts)).strftime("%m%d")
    tail = "".join(rand.choice(BASE32) for _ in range(4))
    return f"{prefix}-{day}-{tail}"


def fingerprint(setup: SetupInput, decimals: int) -> str:
    parts = [
        setup.symbol,
        setup.direction,
        f"{setup.entry_low:.{decimals}f}",
        f"{setup.entry_high:.{decimals}f}",
        f"{setup.stop_loss:.{decimals}f}",
        f"{setup.tp1:.{decimals}f}",
    ]
    return "|".join(parts)


@dataclass
class Event:
    close_ts: float
    timeframe: str
    candle: Candle

    @property
    def order_key(self) -> tuple[float, int]:
        return (self.close_ts, TIMEFRAMES[self.timeframe])


class Engine:
    """Owns every state transition. `context_provider` supplies the live market context."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        context_provider: Callable[[str, float], MarketContext],
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[str, float], str] = new_setup_id,
    ) -> None:
        self.settings = settings
        self.store = store
        self.context_provider = context_provider
        self.clock = clock
        self.id_factory = id_factory

    # ------------------------------------------------------------------ helpers
    @property
    def chat_id(self) -> str:
        return self.settings.telegram_chat_id

    def _decimals(self, symbol: str) -> int:
        return self.settings.symbol(symbol).display_decimals

    def paused(self) -> bool:
        return self.store.get_kv("paused") == "1"

    def set_paused(self, value: bool) -> None:
        self.store.set_kv("paused", "1" if value else "0")

    def _queue(self, conn, setup_id: str, event: str, text: str, now: float) -> None:
        if not self.chat_id:
            return
        self.store.enqueue(conn, f"{setup_id}:{event}", self.chat_id, text, now)

    def load_setup(self, setup_id: str) -> tuple[SetupInput, dict[str, Any]] | None:
        row = self.store.get_setup(setup_id)
        if row is None:
            return None
        return self._decode(row)

    @staticmethod
    def _decode(row) -> tuple[SetupInput, dict[str, Any]]:
        payload = json_loads(row["payload_json"])
        computed = json_loads(row["computed_json"])
        setup = SetupInput(**{k: v for k, v in payload.items() if k != "warnings"})
        setup.warnings = payload.get("warnings", [])
        return setup, computed

    # ------------------------------------------------------------------- intake
    def submit(self, raw: dict[str, Any]) -> dict[str, Any]:
        """`setup_submit`: run the intake gate and arm, reject or report a duplicate."""
        now = self.clock()
        try:
            setup = normalise(raw)
        except ValueError as exc:
            return {
                "status": "REJECTED",
                "setup_id": "",
                "reasons": [{"code": "G-01", "text": f"Fusha të pavlefshme: {exc}"}],
                "warnings": [],
                "computed": {},
                "replaced_setup_id": None,
            }

        symbol = setup.symbol
        if symbol not in self.settings.symbols:
            return {
                "status": "REJECTED",
                "setup_id": "",
                "reasons": [{"code": "G-01", "text": f"Simboli {symbol} nuk mbështetet"}],
                "warnings": [],
                "computed": {},
                "replaced_setup_id": None,
            }

        ctx = self.context_provider(symbol, now)
        decimals = self._decimals(symbol)
        print_id = fingerprint(setup, decimals)

        duplicate = self.store.find_duplicate(print_id, now - self.settings.profile.dedup_window_min * 60)
        if duplicate is not None:
            return {
                "status": "DUPLICATE",
                "setup_id": duplicate["id"],
                "reasons": [],
                "warnings": [],
                "computed": json_loads(duplicate["computed_json"]),
                "replaced_setup_id": None,
            }

        result = evaluate_intake(setup, ctx)

        setup_id = self.id_factory(symbol, now)
        if not result.passed:
            self._store_rejected(setup, result, setup_id, now, ctx)
            return {
                "status": "REJECTED",
                "setup_id": setup_id,
                "reasons": [{"code": f.code, "text": f.text} for f in result.failures],
                "warnings": result.warnings,
                "computed": _public_computed(result.computed, decimals),
                "replaced_setup_id": None,
            }

        replaced = self._arm(setup, result, setup_id, now, ctx, print_id)
        return {
            "status": "ARMED",
            "setup_id": setup_id,
            "reasons": [],
            "warnings": result.warnings,
            "computed": _public_computed(result.computed, decimals),
            "replaced_setup_id": replaced,
        }

    def _store_rejected(
        self, setup: SetupInput, result: IntakeResult, setup_id: str, now: float, ctx: MarketContext
    ) -> None:
        reasons = [f.text for f in result.failures]
        shadow_worthy = {"G-01", "G-02", "G-03", "G-04", "G-05", "G-06", "G-07"}.issubset(
            set(result.computed.get("passed_codes", []))
        )
        with self.store.transaction() as conn:
            self.store.insert_setup(
                conn,
                {
                    "id": setup_id,
                    "symbol": setup.symbol,
                    "direction": setup.direction,
                    "model": setup.entry_model,
                    "state": "REJECTED",
                    "payload_json": json.dumps(setup.to_dict(), separators=(",", ":")),
                    "computed_json": json.dumps(result.computed, separators=(",", ":"), default=str),
                    "fingerprint": fingerprint(setup, self._decimals(setup.symbol)),
                    "created_at": now,
                    "closed_at": now,
                    "last_processed_at": now,
                    "shadow_json": json.dumps({"eligible": shadow_worthy}, separators=(",", ":")),
                },
            )
            self.store.add_event(conn, setup_id, now, "REJECTED", {"reasons": reasons})
            self._queue(
                conn,
                setup_id,
                "REJECTED",
                tg.rejected_message(setup.symbol, setup.direction, reasons, setup_id),
                now,
            )

    def _arm(
        self,
        setup: SetupInput,
        result: IntakeResult,
        setup_id: str,
        now: float,
        ctx: MarketContext,
        print_id: str,
    ) -> str | None:
        decimals = self._decimals(setup.symbol)
        computed = dict(result.computed)
        in_zone = _price_in_zone(setup, ctx)
        state = "IN_ZONE" if in_zone else "ARMED"
        if in_zone:
            computed["tap_at"] = now
            extreme, extreme_ts = _initial_tap_extreme(setup, ctx)
            computed["tap_extreme"] = extreme
            computed["tap_extreme_ts"] = extreme_ts

        replaced_id: str | None = None
        existing = self.store.setups_in_state(ACTIVE_STATES, setup.symbol)
        with self.store.transaction() as conn:
            for row in existing:
                replaced_id = row["id"]
                conn.execute(
                    "UPDATE setups SET state = 'REPLACED', closed_at = ? WHERE id = ?", (now, row["id"])
                )
                self.store.add_event(conn, row["id"], now, "REPLACED", {"by": setup_id})
                self._queue(conn, row["id"], "REPLACED", tg.replaced_message(row["id"], setup_id), now)

            self.store.insert_setup(
                conn,
                {
                    "id": setup_id,
                    "symbol": setup.symbol,
                    "direction": setup.direction,
                    "model": setup.entry_model,
                    "state": state,
                    "payload_json": json.dumps(setup.to_dict(), separators=(",", ":")),
                    "computed_json": json.dumps(computed, separators=(",", ":"), default=str),
                    "fingerprint": print_id,
                    "created_at": now,
                    "armed_at": now,
                    "tap_at": now if in_zone else None,
                    "expires_at": computed.get("expires_at"),
                    "last_processed_at": now,
                    "shadow_json": json.dumps({"eligible": True}, separators=(",", ":")),
                },
            )
            self.store.add_event(conn, setup_id, now, state, {"computed": _short(computed)})
            self._queue(
                conn,
                setup_id,
                "ARMED",
                tg.armed_message(
                    {
                        "symbol": setup.symbol,
                        "direction": setup.direction,
                        "entry_low": setup.entry_low,
                        "entry_high": setup.entry_high,
                        "stop_loss": setup.stop_loss,
                        "tp1": setup.tp1,
                        "tp2": setup.tp2,
                        "tp3": setup.tp3,
                        "rr_plan": computed.get("rr_plan"),
                        "model": setup.entry_model,
                        "ltf": setup.ltf,
                        "waits_for": computed.get("waits_for_sq", ""),
                        "expires": computed.get("expires_at_ny", "-"),
                        "news": ctx.news_blackout,
                        "id": setup_id,
                    },
                    decimals,
                ),
                now,
            )
        return replaced_id

    # ------------------------------------------------------------- event pipeline
    def process_symbol(self, symbol: str, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        ctx = self.context_provider(symbol, now)
        for row in self.store.setups_in_state(NON_TERMINAL, symbol):
            try:
                self.process_setup(row["id"], ctx, now)
            except Exception:  # one bad setup must never stop the engine
                logger.exception("setup %s failed to process", row["id"])

    def process_setup(self, setup_id: str, ctx: MarketContext, now: float) -> None:
        row = self.store.get_setup(setup_id)
        if row is None or row["state"] in TERMINAL or row["outcome"] is not None:
            return  # a settled outcome needs no further tracking
        setup, computed = self._decode(row)
        last_processed = row["last_processed_at"] or row["created_at"]
        events = self._collect_events(setup, ctx, last_processed, now)

        state = row["state"]
        for event in events:
            if state in TERMINAL:
                break
            state = self._apply_event(setup_id, setup, computed, ctx, state, event, now)
        if state not in TERMINAL:
            state = self._apply_quote(setup_id, setup, computed, ctx, state, now)
        self._persist_progress(setup_id, computed, now)

    def _collect_events(
        self, setup: SetupInput, ctx: MarketContext, since: float, now: float
    ) -> list[Event]:
        events: list[Event] = []
        for timeframe in setup.timeframes:
            tf_seconds = TIMEFRAMES[timeframe]
            for candle in ctx.candles(timeframe):
                close_ts = candle.t + tf_seconds
                if since < close_ts <= now:
                    events.append(Event(close_ts, timeframe, candle))
        events.sort(key=lambda e: e.order_key)
        return events

    def _apply_event(
        self,
        setup_id: str,
        setup: SetupInput,
        computed: dict[str, Any],
        ctx: MarketContext,
        state: str,
        event: Event,
        now: float,
    ) -> str:
        replay = (now - event.close_ts) > self.settings.profile.late_trigger_max_s
        if state == "TRIGGERED":
            return self._track_after_entry(setup_id, setup, computed, ctx, event, now)

        # 1. L-01 SL touch on M1 candles
        if event.timeframe == "M1":
            sl = l01_sl_touch(setup, ctx, event.candle)
            if not sl.passed:
                return self._close(setup_id, setup, "INVALIDATED", sl.text, now, replay, event.close_ts)
        # 2. L-02 invalidation close
        if event.timeframe == setup.invalidation_tf:
            inval = l02_invalidation_close(setup, ctx, event.candle)
            if not inval.passed:
                return self._close(setup_id, setup, "INVALIDATED", inval.text, now, replay, event.close_ts)
        # 3. L-03 body respect
        if event.timeframe == setup.respect_tf:
            respect = l03_body_respect(setup, ctx, event.candle)
            if not respect.passed:
                return self._close(setup_id, setup, "INVALIDATED", respect.text, now, replay, event.close_ts)
        # 4. L-04 expiry
        expires_at = computed.get("expires_at")
        if expires_at and event.close_ts >= expires_at:
            return self._close(setup_id, setup, "EXPIRED", "skadoi", now, replay, event.close_ts)

        # 5. tap detection
        if state == "ARMED" and event.timeframe == "M1" and _candle_taps(setup, event.candle):
            state = self._enter_zone(setup_id, setup, computed, ctx, event.candle.t, now)
        if state == "IN_ZONE":
            _update_tap_extreme(setup, computed, ctx, event)

        # 6. trigger evaluation on ltf closes
        if state == "IN_ZONE" and event.timeframe == setup.ltf:
            state = self._evaluate_trigger(setup_id, setup, computed, ctx, event, now)
        return state

    def _apply_quote(
        self,
        setup_id: str,
        setup: SetupInput,
        computed: dict[str, Any],
        ctx: MarketContext,
        state: str,
        now: float,
    ) -> str:
        if ctx.bid is None or ctx.quote_age > self.settings.profile.quote_max_age_s * 3:
            return state
        if state in ACTIVE_STATES:
            sl = l01_sl_touch(setup, ctx, None)
            if not sl.passed:
                return self._close(setup_id, setup, "INVALIDATED", sl.text, now, False, now)
            expires_at = computed.get("expires_at")
            if expires_at and now >= expires_at:
                return self._close(setup_id, setup, "EXPIRED", "skadoi", now, False, now)
            if state == "ARMED" and _price_in_zone(setup, ctx):
                state = self._enter_zone(setup_id, setup, computed, ctx, now, now)
        elif state == "TRIGGERED":
            self._track_quote_outcome(setup_id, setup, computed, ctx, now)
        return state

    def _enter_zone(
        self,
        setup_id: str,
        setup: SetupInput,
        computed: dict[str, Any],
        ctx: MarketContext,
        tap_ts: float,
        now: float,
    ) -> str:
        extreme, extreme_ts = _initial_tap_extreme(setup, ctx, tap_ts)
        computed["tap_at"] = tap_ts
        computed["tap_extreme"] = extreme
        computed["tap_extreme_ts"] = extreme_ts
        with self.store.transaction() as conn:
            conn.execute("UPDATE setups SET state = 'IN_ZONE', tap_at = ? WHERE id = ?", (tap_ts, setup_id))
            self.store.add_event(conn, setup_id, now, "IN_ZONE", {"tap_at": tap_ts, "extreme": extreme})
        return "IN_ZONE"

    def _evaluate_trigger(
        self,
        setup_id: str,
        setup: SetupInput,
        computed: dict[str, Any],
        ctx: MarketContext,
        event: Event,
        now: float,
    ) -> str:
        candles = ctx.candles(setup.ltf)
        k = _index_of(candles, event.candle.t)
        state = _trigger_state(setup, computed, candles)
        if k is None or state is None or k < state.tap_index:
            return "IN_ZONE"

        moment = from_epoch(event.close_ts).astimezone(NY)
        checks: list[tuple[str, bool, str]] = []

        liq_ok = setup.opposing_liquidity_taken or liquidity_taken(
            setup, ctx, self.settings.profile.liq_window_h, until_ts=event.close_ts
        )
        checks.append(("SSL" if setup.is_long else "BSL", liq_ok, "likuiditeti kundërt s'është marrë"))

        confirm_ok, confirm_note = self._model_confirmation(setup, ctx, candles, state, k)
        checks.append(("Konfirmim", confirm_ok, confirm_note))

        fresh = t04_freshness(ctx, state, k)
        checks.append(("Freskia", fresh.passed, fresh.text))

        time_check = t05_time(setup, ctx, moment, self.settings.btc_weekend_entries)
        checks.append(("Koha", time_check.passed, time_check.text))

        data_check = t08_data(ctx, setup, state, event.candle.t)
        conflict = self._conflicting_trigger(setup)

        # T-01…T-05, T-08 and T-09 only hold the setup back; MISSED is reserved for the cases
        # validation-rules §7 names: a spread spike over two bars, a price that ran away, a
        # confirmation that happened during an outage, and an active pause.
        core_ok = liq_ok and confirm_ok and fresh.passed and time_check.passed and data_check.passed
        if not core_ok or conflict:
            return "IN_ZONE"

        if self.paused():
            return self._close(setup_id, setup, "MISSED", "pauzë aktive", now, False, event.close_ts)

        delay = max(0.0, now - event.close_ts)
        if delay > self.settings.profile.late_trigger_max_s:
            return self._close(
                setup_id, setup, "MISSED", "konfirmimi ndodhi gjatë ndërprerjes", now, True, event.close_ts
            )

        spread = t06_spread(ctx)
        if not spread.passed:
            streak = int(computed.get("spread_fail_streak", 0)) + 1
            computed["spread_fail_streak"] = streak
            if streak >= 2:
                return self._close(setup_id, setup, "MISSED", spread.text, now, False, event.close_ts)
            return "IN_ZONE"
        computed["spread_fail_streak"] = 0

        price = t07_price(setup, ctx)
        if not price.passed:
            if "iku" in price.text:
                return self._close(setup_id, setup, "MISSED", price.text, now, False, event.close_ts)
            return "IN_ZONE"

        score, breakdown = compute_score(
            setup,
            ctx,
            candles,
            state,
            k,
            moment,
            holiday_today=self._holiday_today(ctx),
            pda_unverified=computed.get("pda_check", {}).get("status") == "UNVERIFIED",
        )
        if score < self.settings.profile.score_min:
            return "IN_ZONE"

        entry = price.data["entry"]
        limit = price.data["chase_limit"]
        checks.append(("Score", True, ""))
        return self._trigger(
            setup_id,
            setup,
            computed,
            ctx,
            entry,
            limit,
            score,
            breakdown,
            time_check.data.get("window", ""),
            delay,
            now,
            checks,
        )

    def _model_confirmation(
        self,
        setup: SetupInput,
        ctx: MarketContext,
        candles: list[Candle],
        state: TriggerState,
        k: int,
    ) -> tuple[bool, str]:
        """T-02/T-03 or the model override (validation-rules §8)."""
        model = setup.entry_model
        if model == "VENOM":
            ref = setup.model_ref_level
            entry_price = ctx.ask if setup.is_long else ctx.bid
            if ref is None or entry_price is None:
                return False, "çmimi nuk arriti mbylljen BISI"
            reached = entry_price <= ref if setup.is_long else entry_price >= ref
            return reached, "çmimi nuk arriti mbylljen BISI"
        if model == "TURTLE_SOUP_DEFERRED":
            return _rejection_candle(setup, ctx, candles, state, k)
        if model == "IOFED":
            return _iofed_confirmation(setup, ctx, candles, state, k)

        cisd = t02_cisd(setup, ctx, candles, state, k)
        if not cisd.passed:
            return False, cisd.text
        disp = t03_displacement(setup, ctx, candles, state, k)
        if not disp.passed:
            return False, disp.text
        if model == "MODEL_2":
            six = ctx.level("six_am_open")
            entry_price = ctx.ask if setup.is_long else ctx.bid
            if six is None or entry_price is None:
                return False, "6 AM open mungon"
            good = entry_price < six if setup.is_long else entry_price > six
            if not good:
                return False, "çmimi s'është në anën e duhur të 6 AM open"
        if model == "SILVER_BULLET":
            try:
                formed = parse_ny(setup.pda_formed_at_ny)
            except (ValueError, TypeError):
                return False, "koha e formimit e pavlefshme"
            from app.timeutil import in_model_windows

            close_at = from_epoch(candles[k].t + TIMEFRAMES[setup.ltf])
            window = in_model_windows("SILVER_BULLET", formed, ctx.profile.name == "BALANCED", close_at)
            if window is None:
                return False, "jashtë dritares Silver Bullet"
        return True, ""

    def _conflicting_trigger(self, setup: SetupInput) -> bool:
        """T-09: an unresolved TRIGGERED setup in the opposite direction on the same symbol."""
        rows = self.store.setups_in_state(("TRIGGERED",), setup.symbol)
        return any(row["direction"] != setup.direction for row in rows)

    def _holiday_today(self, ctx: MarketContext) -> bool:
        return bool(ctx.levels.get("post_holiday"))

    def _trigger(
        self,
        setup_id: str,
        setup: SetupInput,
        computed: dict[str, Any],
        ctx: MarketContext,
        entry: float,
        limit: float,
        score: int,
        breakdown: list[str],
        window: str,
        delay: float,
        now: float,
        checks: list[tuple[str, bool, str]],
    ) -> str:
        decimals = self._decimals(setup.symbol)
        risk = abs(entry - setup.stop_loss)
        rr = abs(setup.primary_tp - entry) / risk if risk > 0 else 0.0
        valid_until = ny_string(now + self.settings.profile.enter_valid_min * 60)
        computed["entry_price"] = entry
        computed["chase_limit"] = limit
        computed["score"] = score
        computed["score_breakdown"] = breakdown
        computed["entry_fvgs"] = [
            {"low": f.low, "high": f.high, "formed_at": f.formed_at} for f in collect_entry_fvgs(setup, ctx)
        ]
        computed["exits_sent"] = []
        computed["tps_hit"] = []
        text = tg.enter_message(
            {
                "symbol": setup.symbol,
                "direction": setup.direction,
                "entry": entry,
                "spread": ctx.spread,
                "stop_loss": setup.stop_loss,
                "tp1": setup.tp1,
                "tp2": setup.tp2,
                "tp3": setup.tp3,
                "rr": rr,
                "target_label": "TP2" if setup.tp2 is not None else "TP1",
                "chase_limit": limit,
                "valid_until": valid_until,
                "model": setup.entry_model,
                "ltf": setup.ltf,
                "window": window,
                "checks": " · ".join(f"{name} {'✓' if ok else '✗'}" for name, ok, _ in checks),
                "score": score,
                "pda_unverified": computed.get("pda_check", {}).get("status") == "UNVERIFIED",
                "news_down": not ctx.news_feed_ok,
                "delay_s": int(delay) if delay >= 5 else 0,
                "id": setup_id,
                "time": ny_string(now),
            },
            decimals,
        )
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE setups SET state = 'TRIGGERED', triggered_at = ?, entry_price = ?, score = ?, "
                "computed_json = ?, last_processed_at = ? WHERE id = ?",
                (now, entry, score, json.dumps(computed, separators=(",", ":"), default=str), now, setup_id),
            )
            self.store.add_event(
                conn, setup_id, now, "TRIGGERED", {"entry": entry, "score": score, "breakdown": breakdown}
            )
            self._queue(conn, setup_id, "ENTER", text, now)
        return "TRIGGERED"

    def _close(
        self,
        setup_id: str,
        setup: SetupInput,
        state: str,
        reason: str,
        now: float,
        replay: bool,
        at_ts: float,
    ) -> str:
        if state == "INVALIDATED":
            text = tg.invalidated_message(
                setup.symbol, setup.direction, reason, setup_id, ny_string(at_ts), replay
            )
            event_key = "INVALIDATED"
        elif state == "EXPIRED":
            text = tg.expired_message(setup.symbol, setup.direction, setup_id, ny_string(at_ts))
            event_key = "EXPIRED"
        elif state == "MISSED":
            text = tg.missed_message(setup.symbol, setup.direction, reason, setup_id)
            event_key = "MISSED"
        elif state == "CANCELLED":
            text = tg.cancelled_message(setup_id)
            event_key = "CANCELLED"
        else:  # pragma: no cover - guarded by callers
            raise ValueError(state)
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE setups SET state = ?, closed_at = ?, last_processed_at = ? WHERE id = ?",
                (state, now, now, setup_id),
            )
            self.store.add_event(conn, setup_id, now, state, {"reason": reason, "replay": replay})
            self._queue(conn, setup_id, event_key, text, now)
        return state

    def _persist_progress(self, setup_id: str, computed: dict[str, Any], now: float) -> None:
        self.store.execute(
            "UPDATE setups SET computed_json = ?, last_processed_at = ? WHERE id = ? AND state NOT IN "
            "('REJECTED','REPLACED','CANCELLED')",
            (json.dumps(computed, separators=(",", ":"), default=str), now, setup_id),
        )

    # ------------------------------------------------------------- after ENTER
    def _track_after_entry(
        self,
        setup_id: str,
        setup: SetupInput,
        computed: dict[str, Any],
        ctx: MarketContext,
        event: Event,
        now: float,
    ) -> str:
        sent: list[str] = list(computed.get("exits_sent", []))
        # LP-01 exit on an invalidation-timeframe close beyond the invalidation level
        if event.timeframe == setup.invalidation_tf and "INVALIDATION" not in sent:
            if beyond(event.candle.c, setup.invalidation_level, setup.sign):
                self._emit_exit(setup_id, setup, computed, "mbyllje përtej invalidimit", "INVALIDATION", now)
                sent.append("INVALIDATION")
        # LP-02 three same-direction arrays broken
        if event.timeframe == setup.ltf and "ARRAYS" not in sent:
            if _three_arrays_broken(setup, computed, ctx):
                self._emit_exit(setup_id, setup, computed, "tri array-t u thyen", "ARRAYS", now)
                sent.append("ARRAYS")
        computed["exits_sent"] = sent
        # LP-03 outcome on M1
        if event.timeframe == "M1":
            self._track_outcome(setup_id, setup, computed, ctx, event.candle, now)
        return "TRIGGERED"

    def _emit_exit(
        self, setup_id: str, setup: SetupInput, computed: dict[str, Any], reason: str, key: str, now: float
    ) -> None:
        with self.store.transaction() as conn:
            self.store.add_event(conn, setup_id, now, "EXIT", {"reason": reason})
            self._queue(
                conn,
                setup_id,
                f"EXIT_{key}",
                tg.exit_message(setup.symbol, setup.direction, reason, setup_id),
                now,
            )

    def _track_outcome(
        self,
        setup_id: str,
        setup: SetupInput,
        computed: dict[str, Any],
        ctx: MarketContext,
        candle: Candle,
        now: float,
    ) -> None:
        """LP-03: SL first when the same M1 candle touches both (failure mode E8)."""
        spread = ctx.median_spread()
        entry = computed.get("entry_price") or setup.entry_ref
        risk = abs(entry - setup.stop_loss) or 1.0
        hit_sl = candle.l <= setup.stop_loss if setup.is_long else (candle.h + spread) >= setup.stop_loss
        tps_hit: list[int] = list(computed.get("tps_hit", []))
        targets = setup.targets
        reached: list[int] = []
        for index, target in enumerate(targets, start=1):
            if index in tps_hit:
                continue
            if setup.is_long:
                if candle.h >= target:
                    reached.append(index)
            elif (candle.l - spread) <= target:
                reached.append(index)

        if hit_sl:
            self._finish(setup_id, setup, computed, "SL", tg.sl_message(setup.symbol, setup.direction, setup_id), now)
            return
        for index in reached:
            target = targets[index - 1]
            r_multiple = abs(target - entry) / risk
            tps_hit.append(index)
            with self.store.transaction() as conn:
                self.store.add_event(conn, setup_id, now, "TP", {"n": index, "r": r_multiple})
                self._queue(
                    conn,
                    setup_id,
                    f"TP{index}",
                    tg.tp_message(setup.symbol, setup.direction, index, r_multiple, setup_id),
                    now,
                )
        computed["tps_hit"] = tps_hit
        if tps_hit and len(tps_hit) == len(targets):
            self._finish(setup_id, setup, computed, f"TP{max(tps_hit)}", None, now)
            return
        triggered_at = self.store.get_setup(setup_id)["triggered_at"] or now
        if now >= self._outcome_horizon(setup, triggered_at):
            self._finish(
                setup_id,
                setup,
                computed,
                "TIMEOUT",
                tg.timeout_message(setup.symbol, setup.direction, setup_id),
                now,
            )

    def _outcome_horizon(self, setup: SetupInput, triggered_at: float) -> float:
        """`OUTCOME_HORIZON_H`, clamped to Thursday 10:00 for Model 2 and to Friday's close for gold."""
        horizon = triggered_at + self.settings.profile.outcome_horizon_h * 3600
        start = from_epoch(triggered_at)
        if setup.entry_model == "MODEL_2":
            horizon = min(horizon, next_weekday_at(start, weekday=3, minutes=10 * 60).timestamp())
        if setup.symbol.upper() == "XAUUSD":
            cutoff = friday_close(start, self.settings.profile.friday_cutoff_ny)
            if cutoff is not None:
                horizon = min(horizon, cutoff.timestamp())
        return horizon

    def _track_quote_outcome(
        self,
        setup_id: str,
        setup: SetupInput,
        computed: dict[str, Any],
        ctx: MarketContext,
        now: float,
    ) -> None:
        if ctx.bid is None:
            return
        synthetic = Candle(now, ctx.bid, ctx.bid, ctx.bid, ctx.bid)
        self._track_outcome(setup_id, setup, computed, ctx, synthetic, now)

    def _finish(
        self,
        setup_id: str,
        setup: SetupInput,
        computed: dict[str, Any],
        outcome: str,
        message: str | None,
        now: float,
    ) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE setups SET outcome = ?, closed_at = ?, computed_json = ?, last_processed_at = ? WHERE id = ?",
                (outcome, now, json.dumps(computed, separators=(",", ":"), default=str), now, setup_id),
            )
            self.store.add_event(conn, setup_id, now, "OUTCOME", {"outcome": outcome})
            if message:
                self._queue(conn, setup_id, f"OUTCOME_{outcome}", message, now)

    # --------------------------------------------------------------- commands
    def cancel(self, setup_id: str) -> dict[str, Any]:
        row = self.store.get_setup(setup_id)
        if row is None:
            return {"status": "NOT_FOUND", "setup_id": setup_id}
        if row["state"] == "TRIGGERED":
            return {"status": "CANNOT_CANCEL", "setup_id": setup_id, "state": row["state"]}
        if row["state"] not in ACTIVE_STATES:
            return {"status": "ALREADY_CLOSED", "setup_id": setup_id, "state": row["state"]}
        setup, _computed = self._decode(row)
        now = self.clock()
        self._close(setup_id, setup, "CANCELLED", "kërkesë", now, False, now)
        return {"status": "CANCELLED", "setup_id": setup_id}

    def status(self, setup_id: str | None = None) -> dict[str, Any]:
        if setup_id:
            row = self.store.get_setup(setup_id)
            if row is None:
                return {"error": "setup_id nuk ekziston", "setup_id": setup_id}
            setup, computed = self._decode(row)
            ctx = self.context_provider(row["symbol"], self.clock())
            decimals = self._decimals(row["symbol"])
            bid = ctx.bid
            return {
                "setup_id": setup_id,
                "state": row["state"],
                "symbol": row["symbol"],
                "direction": row["direction"],
                "model": row["model"],
                "outcome": row["outcome"],
                "live_bid": round(bid, decimals) if bid else None,
                "distance_to_zone": None
                if bid is None
                else round(min(abs(bid - setup.entry_low), abs(bid - setup.entry_high)), decimals),
                "distance_to_sl": None if bid is None else round(abs(bid - setup.stop_loss), decimals),
                "distance_to_tps": []
                if bid is None
                else [round(abs(bid - t), decimals) for t in setup.targets],
                "expires_at_ny": computed.get("expires_at_ny"),
                "waits_for_sq": computed.get("waits_for_sq"),
                "timeline": [
                    {"ts": ny_string(row2["ts"]), "type": row2["type"], "data": json_loads(row2["data_json"])}
                    for row2 in self.store.events_for(setup_id, 20)
                ],
            }
        active = self.store.setups_in_state(NON_TERMINAL)
        closed = [r for r in self.store.recent_setups(30) if r["state"] in TERMINAL][:10]
        return {
            "active": [_row_summary(r) for r in active],
            "recent_closed": [_row_summary(r) for r in closed],
            "paused": self.paused(),
        }

    # ------------------------------------------------------- shadow and stats
    def compute_shadow(self, setup_id: str, ctx: MarketContext) -> str | None:
        """Blind-limit outcome the owner would have had before this system (validation-rules §11)."""
        row = self.store.get_setup(setup_id)
        if row is None:
            return None
        shadow = json_loads(row["shadow_json"], {})
        if shadow.get("result"):
            return shadow["result"]
        if not shadow.get("eligible", False):
            return None
        setup, _computed = self._decode(row)
        created = row["created_at"]
        horizon = created + 24 * 3600
        edge = setup.entry_high if setup.is_long else setup.entry_low
        candles = [c for c in ctx.candles("M1") if created <= c.t <= horizon]
        filled = False
        result = "NO_FILL"
        for candle in candles:
            if not filled:
                touched = candle.l <= edge if setup.is_long else candle.h >= edge
                if touched:
                    filled = True
                else:
                    continue
            hit_sl = candle.l <= setup.stop_loss if setup.is_long else candle.h >= setup.stop_loss
            hit_tp = candle.h >= setup.tp1 if setup.is_long else candle.l <= setup.tp1
            if hit_sl:
                result = "LOSS"
                break
            if hit_tp:
                result = "WIN"
                break
        if result == "NO_FILL" and self.clock() < horizon:
            return None  # still running; decide once the horizon has passed
        if filled and result == "NO_FILL":
            # Filled but neither target nor stop was reached inside the window: no verdict either way.
            result = "UNRESOLVED"
        shadow["result"] = result
        self.store.execute(
            "UPDATE setups SET shadow_json = ? WHERE id = ?",
            (json.dumps(shadow, separators=(",", ":")), setup_id),
        )
        return result

    def evaluate_pending_shadows(self, limit: int = 20) -> int:
        """Settle the blind-limit comparison for setups whose 24-hour window has closed."""
        cutoff = self.clock() - 24 * 3600
        rows = self.store.query(
            "SELECT id, symbol, shadow_json FROM setups WHERE created_at < ? ORDER BY created_at LIMIT ?",
            (cutoff, limit * 5),
        )
        settled = 0
        for row in rows:
            shadow = json_loads(row["shadow_json"], {})
            if shadow.get("result") or not shadow.get("eligible"):
                continue
            ctx = self.context_provider(row["symbol"], self.clock())
            if self.compute_shadow(row["id"], ctx):
                settled += 1
            if settled >= limit:
                break
        return settled

    def stats(self, days: int = 7) -> dict[str, Any]:
        since = self.clock() - days * 86400
        rows = self.store.query("SELECT * FROM setups WHERE created_at >= ?", (since,))
        counts = {"n": len(rows), "rej": 0, "inv": 0, "exp": 0, "mis": 0, "ent": 0, "win": 0, "loss": 0, "open": 0}
        saves = missed_wins = blind_w = blind_l = 0
        for row in rows:
            state, outcome = row["state"], row["outcome"]
            shadow = json_loads(row["shadow_json"], {}).get("result")
            if state == "REJECTED":
                counts["rej"] += 1
            elif state == "INVALIDATED":
                counts["inv"] += 1
            elif state == "EXPIRED":
                counts["exp"] += 1
            elif state == "MISSED":
                counts["mis"] += 1
            if state == "TRIGGERED":
                counts["ent"] += 1
                if outcome and outcome.startswith("TP"):
                    counts["win"] += 1
                elif outcome == "SL":
                    counts["loss"] += 1
                else:
                    counts["open"] += 1
            elif shadow == "LOSS":
                saves += 1
            elif shadow == "WIN":
                missed_wins += 1
            if shadow == "WIN":
                blind_w += 1
            elif shadow == "LOSS":
                blind_l += 1
        counts.update(
            {"saves": saves, "missed_wins": missed_wins, "blind_w": blind_w, "blind_l": blind_l, "days": days}
        )
        return counts


# ------------------------------------------------------------------- helpers
def _price_in_zone(setup: SetupInput, ctx: MarketContext) -> bool:
    bid = ctx.bid
    if bid is None:
        return False
    return bid <= setup.entry_high if setup.is_long else bid >= setup.entry_low


def _candle_taps(setup: SetupInput, candle: Candle) -> bool:
    return candle.l <= setup.entry_high if setup.is_long else candle.h >= setup.entry_low


def _initial_tap_extreme(
    setup: SetupInput, ctx: MarketContext, tap_ts: float | None = None
) -> tuple[float, float]:
    """The ltf bar that holds the tap: the newest bar whose open is at or before the tap."""
    moment = ctx.now_ts if tap_ts is None else tap_ts
    candles = [c for c in ctx.candles(setup.ltf) if c.t <= moment]
    if not candles:
        price = ctx.bid or setup.entry_ref
        return price, moment
    last = candles[-1]
    return (last.l, last.t) if setup.is_long else (last.h, last.t)


def _update_tap_extreme(setup: SetupInput, computed: dict[str, Any], ctx: MarketContext, event: Event) -> None:
    if event.timeframe != setup.ltf:
        return
    current = computed.get("tap_extreme")
    candle = event.candle
    value = candle.l if setup.is_long else candle.h
    if current is None or (value < current if setup.is_long else value > current):
        computed["tap_extreme"] = value
        computed["tap_extreme_ts"] = candle.t


def _index_of(candles: list[Candle], open_ts: float) -> int | None:
    for i in range(len(candles) - 1, -1, -1):
        if candles[i].t == open_ts:
            return i
    return None


def _trigger_state(setup: SetupInput, computed: dict[str, Any], candles: list[Candle]) -> TriggerState | None:
    extreme_ts = computed.get("tap_extreme_ts")
    extreme = computed.get("tap_extreme")
    if extreme_ts is None or extreme is None:
        return None
    index = _index_of(candles, extreme_ts)
    if index is None:
        return None
    # tap_ts is the open time of the bar holding the extreme: T-08 walks the ltf grid from it.
    return TriggerState(tap_index=index, tap_extreme=extreme, tap_ts=candles[index].t)


def _rejection_candle(
    setup: SetupInput, ctx: MarketContext, candles: list[Candle], state: TriggerState, k: int
) -> tuple[bool, str]:
    """Turtle Soup Deferred (v11 §5.4.1): wick into the zone, body back outside."""
    candle = candles[k]
    tol = ctx.tol(setup.ltf)
    ce = setup.pda_ce
    if setup.is_long:
        wicked = candle.l <= setup.entry_high and candle.l >= ce - tol
        body_out = candle.body_bot > setup.entry_high
    else:
        wicked = candle.h >= setup.entry_low and candle.h <= ce + tol
        body_out = candle.body_top < setup.entry_low
    if not (wicked and body_out):
        return False, "qiri refuzimi mungon"
    breach_index = max(0, state.tap_index - ctx.profile.confirm_max_bars)
    atr = ctx.atr(setup.ltf) or 0.0
    from app.market import displacement as is_displacement

    for candle_i in candles[breach_index : k + 1]:
        aligned = candle_i.bullish if setup.is_long else candle_i.bearish
        if aligned and is_displacement(candle_i, atr, ctx.profile.disp_body_atr, ctx.profile.disp_body_range):
            return True, ""
    return False, "displacement mungon pas thyerjes"


def _iofed_confirmation(
    setup: SetupInput, ctx: MarketContext, candles: list[Candle], state: TriggerState, k: int
) -> tuple[bool, str]:
    """IOFED (v11 §5.7): a new same-direction FVG inside the PDA zone, triggering at its m+1 close."""
    direction = "BULL" if setup.is_long else "BEAR"
    for f in find_fvgs(candles):
        if f.direction != direction or f.index < state.tap_index or f.index + 1 != k:
            continue
        if f.high >= setup.pda_low and f.low <= setup.pda_high:
            return True, ""
    return False, "s'ka FVG të re brenda zonës"


def _three_arrays_broken(setup: SetupInput, computed: dict[str, Any], ctx: MarketContext) -> bool:
    arrays = computed.get("entry_fvgs", [])
    if len(arrays) < 3:
        return False
    candles = ctx.candles(setup.ltf)
    broken = 0
    for array in arrays:
        far = array["low"] if setup.is_long else array["high"]
        if any(c.t > array["formed_at"] and beyond(c.c, far, setup.sign) for c in candles):
            broken += 1
    return broken >= 3


def _row_summary(row) -> dict[str, Any]:
    computed = json_loads(row["computed_json"])
    return {
        "setup_id": row["id"],
        "symbol": row["symbol"],
        "direction": row["direction"],
        "model": row["model"],
        "state": row["state"],
        "outcome": row["outcome"],
        "created_ny": ny_string(row["created_at"]),
        "expires_at_ny": computed.get("expires_at_ny"),
    }


def _short(computed: dict[str, Any]) -> dict[str, Any]:
    keys = ("rr_plan", "hard_level", "expires_at_ny", "windows", "waits_for_sq")
    return {k: computed.get(k) for k in keys}


def _public_computed(computed: dict[str, Any], decimals: int) -> dict[str, Any]:
    out = {
        "ce": computed.get("ce"),
        "entry_ref": computed.get("entry_ref"),
        "rr_plan": computed.get("rr_plan"),
        "hard_level": computed.get("hard_level"),
        "hard_level_name": computed.get("hard_level_name"),
        "atr": computed.get("atr", {}),
        "tol": computed.get("tol"),
        "pda_check": computed.get("pda_check", {}),
        "liquidity_check": computed.get("liquidity_check", {}),
        "expires_at_ny": computed.get("expires_at_ny"),
        "windows": computed.get("windows", []),
        "waits_for_sq": computed.get("waits_for_sq", ""),
    }
    for key in ("ce", "entry_ref", "hard_level", "tol"):
        if isinstance(out.get(key), float):
            out[key] = round(out[key], decimals + 2)
    atr = out.get("atr") or {}
    out["atr"] = {k: (round(v, decimals + 2) if isinstance(v, float) else v) for k, v in atr.items()}
    return out

