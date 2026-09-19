"""The universal validator's state machine.

One job: watch a registered setup against live data and say ENTER NOW, or LIMIT at a recomputed
price, then protect the profit. Two cancellations exist and no others (docs/VALIDATOR.md §1.3).

Every transition writes the setup row, its event and its Telegram message inside one transaction, so
a restart can never lose a decision or send it twice.
"""

from __future__ import annotations

import json
import random
import string
import time
from collections.abc import Callable
from typing import Any

from app import plan as planning
from app import telegram as tg
from app.config import Settings
from app.context import MarketContext
from app.evidence import SCORE_MIN, Verdict, evaluate, window, zone_failed
from app.market import swing_high_indices, swing_low_indices
from app.setup_model import Setup, UnusableSetup, normalise
from app.store import Store, json_loads
from app.timeutil import ny_string

WATCHING = "WATCHING"
AT_ZONE = "AT_ZONE"
LIMIT = "LIMIT"
ENTERED = "ENTERED"
DONE = "DONE"
OPEN_STATES = (WATCHING, AT_ZONE, LIMIT, ENTERED)

APPROACH_ATR_M5 = 0.5  # how close price must come before the "approaching" note
PROGRESS_THROTTLE_S = 300.0  # at most one progress message per setup per five minutes
BE_R = 1.0


def new_setup_id(symbol: str, now_ts: float, rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    tag = "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    return f"{symbol[:3].upper()}-{time.strftime('%m%d', time.gmtime(now_ts))}-{tag}"


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
        # Where the market was the last time each setup was looked at, so one pass can ask what the
        # price did in between instead of only where it happens to be now.
        self._last_seen: dict[str, tuple[float, float]] = {}

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

    @staticmethod
    def _decode(row) -> tuple[Setup, dict[str, Any]]:
        return Setup.from_dict(json_loads(row["payload_json"])), json_loads(row["computed_json"])

    def load_setup(self, setup_id: str) -> tuple[Setup, dict[str, Any]] | None:
        row = self.store.get_setup(setup_id)
        return None if row is None else self._decode(row)

    # ------------------------------------------------------------------- intake
    def submit(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Accept anything. The only failure is a payload without numbers to watch."""
        now = self.clock()
        symbol = str(raw.get("symbol") or "XAUUSD").upper()
        ctx = self.context_provider(symbol, now)
        try:
            setup = normalise(raw, zone_pad=planning.zone_pad(ctx))
        except UnusableSetup as exc:
            return {"status": "unusable", "reason": str(exc)}

        setup_id = self.id_factory(symbol, now)
        decimals = self._decimals(symbol)
        price = ctx.bid
        distance = tp1_distance = None
        if price is not None:
            distance = max(0.0, setup.zone_low - price if price < setup.zone_low else price - setup.zone_high)
            if setup.tp1 is not None:
                tp1_distance = abs(price - setup.tp1)
        computed = {
            "touch_ts": None,
            "progress_at": 0.0,
            "seen": [],
            "approached": False,
            "plan": None,
            "entry_why": "",
            "secure_done": False,
            "be_done": False,
            "reversal_done": False,
            "tps_done": [],
        }
        card = {
            **setup.as_dict(),
            "setup_id": setup_id,
            "stop": setup.stop_loss,
            "risk": setup.risk,
            "rr": planning.rr_for(setup.zone_mid, setup.stop_loss, setup.targets),
            "price": price,
            "distance": distance,
            "tp1_distance": tp1_distance,
        }
        with self.store.transaction() as conn:
            self.store.insert_setup(
                conn,
                {
                    "id": setup_id,
                    "symbol": symbol,
                    "direction": setup.direction,
                    "model": setup.label or "UNIVERSAL",
                    "state": WATCHING,
                    "payload_json": json.dumps(setup.as_dict(), default=str),
                    "computed_json": json.dumps(computed, default=str),
                    "fingerprint": f"{symbol}:{setup.direction}:{round(setup.zone_mid, 4)}",
                    "created_at": now,
                    "armed_at": now,
                    "last_processed_at": now,
                },
            )
            self.store.add_event(conn, setup_id, now, "REGISTERED", card)
            self._queue(conn, setup_id, "registered", tg.registered_message(card, decimals), now)
        return {"status": "registered", "setup_id": setup_id, "setup": setup.as_dict(), "notes": setup.notes}

    # ------------------------------------------------------------------ the loop
    def process_symbol(self, symbol: str, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        rows = self.store.setups_in_state(OPEN_STATES, symbol)
        if not rows:
            return
        ctx = self.context_provider(symbol, now)
        for row in rows:
            self.process_setup(row["id"], ctx, now)

    def _travelled(self, setup_id: str, ctx: MarketContext, price: float, created_at: float, now: float):
        """The range the market actually covered since this setup was last looked at.

        A poll sees a point. Between two polls — or behind a data outage — price can cross a whole
        zone and keep going, and the zone was then never "touched": the setup went from WATCHING
        straight to a cancellation, with no word about the band it had walked through. The closed
        M1 candles since the last pass carry the extremes the polls missed.
        """
        low = high = price
        previous = self._last_seen.get(setup_id)
        since = max(created_at, previous[0] if previous else now - 60.0)
        if previous:
            low, high = min(low, previous[1]), max(high, previous[1])
        for candle in ctx.candles("M1"):
            if candle.t >= since:  # bars that opened after the last pass: never pre-setup history
                low, high = min(low, candle.l), max(high, candle.h)
        self._last_seen[setup_id] = (now, price)
        return low, high

    def process_setup(self, setup_id: str, ctx: MarketContext, now: float) -> None:
        row = self.store.get_setup(setup_id)
        if row is None or row["state"] not in OPEN_STATES:
            return
        setup, computed = self._decode(row)
        state = row["state"]
        if ctx.bid is None:
            return
        price = ctx.bid
        band = self._travelled(setup_id, ctx, price, row["created_at"], now)

        if state in (WATCHING, AT_ZONE, LIMIT):
            if self._cancel_if_overtaken(setup, setup_id, ctx, price, now, band):
                return
        if state in (WATCHING, AT_ZONE):
            self._before_entry(setup, setup_id, state, computed, ctx, price, now, band)
        elif state == LIMIT:
            self._await_fill(setup, setup_id, computed, ctx, price, now)
        elif state == ENTERED:
            self._manage(setup, setup_id, computed, ctx, price, now)

    # -------------------------------------------------------------- pre-entry
    def _cancel_if_overtaken(
        self, setup: Setup, setup_id: str, ctx: MarketContext, price: float, now: float, band: tuple[float, float]
    ) -> bool:
        """The only two cancellations: the stop or TP1 reached before the entry was ever touched.

        Judged on the range price covered, not on the poll's own price: a level crossed between two
        passes is still a level crossed.
        """
        low, high = band
        hit_stop = low <= setup.stop_loss if setup.is_long else high >= setup.stop_loss
        tp1 = setup.tp1
        hit_tp1 = tp1 is not None and (high >= tp1 if setup.is_long else low <= tp1)
        reason = "SL_FIRST" if hit_stop else "TP1_FIRST" if hit_tp1 else ""
        if not reason:
            return False
        data = {
            "symbol": setup.symbol,
            "direction": setup.direction,
            "setup_id": setup_id,
            "reason": reason,
            "price": price,
            # Both in one window means the order is unknowable, and the owner is told exactly that
            # rather than being left to guess why a zone that was reached produced no entry.
            "zone_crossed": low <= setup.zone_high and high >= setup.zone_low,
        }
        text = tg.cancel_message(data, self._decimals(setup.symbol))
        self._close(setup_id, f"CANCELLED_{reason}", now, data, text)
        return True

    def _before_entry(
        self,
        setup: Setup,
        setup_id: str,
        state: str,
        computed: dict[str, Any],
        ctx: MarketContext,
        price: float,
        now: float,
        band: tuple[float, float],
    ) -> None:
        # The zone counts as touched when the range price covered since the last pass overlaps it,
        # not only when a poll lands inside a band a fast market crosses in seconds.
        in_zone = band[0] <= setup.zone_high and band[1] >= setup.zone_low
        decimals = self._decimals(setup.symbol)
        base = {"symbol": setup.symbol, "direction": setup.direction, "setup_id": setup_id, "price": price}

        if state == WATCHING:
            if not in_zone:
                self._approach_note(setup, setup_id, computed, ctx, price, now, base, decimals)
                return
            computed["touch_ts"] = now
            touched = {**base, "spread": ctx.spread}
            with self.store.transaction() as conn:
                conn.execute(
                    "UPDATE setups SET state = ?, tap_at = ?, computed_json = ? WHERE id = ?",
                    (AT_ZONE, now, json.dumps(computed, default=str), setup_id),
                )
                self.store.add_event(conn, setup_id, now, "ZONE_TOUCH", base)
                self._queue(conn, setup_id, "touch", tg.touch_message(touched, decimals), now)
            return

        touch_ts = float(computed.get("touch_ts") or now)
        if zone_failed(setup, ctx, touch_ts):
            self._zone_failed(setup, setup_id, computed, now, base, decimals)
            return
        verdict = evaluate(setup, ctx, touch_ts)
        if verdict.ready and not self.paused():
            self._decide(setup, setup_id, computed, ctx, verdict, price, touch_ts, now)
            return
        self._evidence_note(setup, setup_id, computed, verdict, now, base)

    def _zone_failed(
        self,
        setup: Setup,
        setup_id: str,
        computed: dict[str, Any],
        now: float,
        base: dict[str, Any],
        decimals: int,
    ) -> None:
        """Price closed through the zone: the reaction it was building no longer exists.

        The setup is not cancelled — only the stop and TP1 do that. But evidence describing a defence
        that failed must not be carried forward, so the watch starts over from the next touch.
        """
        computed["touch_ts"] = None
        computed["seen"] = []
        computed["progress_at"] = 0.0
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE setups SET state = ?, tap_at = NULL, computed_json = ?, score = 0 WHERE id = ?",
                (WATCHING, json.dumps(computed, default=str), setup_id),
            )
            self.store.add_event(conn, setup_id, now, "ZONE_FAILED", base)
            self._queue(conn, setup_id, f"failed:{int(now)}", tg.zone_failed_message(base, decimals), now)

    def _approach_note(
        self,
        setup: Setup,
        setup_id: str,
        computed: dict[str, Any],
        ctx: MarketContext,
        price: float,
        now: float,
        base: dict[str, Any],
        decimals: int,
    ) -> None:
        if computed.get("approached"):
            return
        atr5 = ctx.atr("M5") or 0.0
        distance = setup.zone_low - price if price < setup.zone_low else price - setup.zone_high
        if atr5 <= 0 or distance > APPROACH_ATR_M5 * atr5:
            return
        computed["approached"] = True
        payload = {**base, "zone_low": setup.zone_low, "zone_high": setup.zone_high, "distance": distance}
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE setups SET computed_json = ? WHERE id = ?", (json.dumps(computed, default=str), setup_id)
            )
            self.store.add_event(conn, setup_id, now, "APPROACH", payload)
            self._queue(conn, setup_id, "approach", tg.approach_message(payload, decimals), now)

    def _evidence_note(
        self,
        setup: Setup,
        setup_id: str,
        computed: dict[str, Any],
        verdict: Verdict,
        now: float,
        base: dict[str, Any],
    ) -> None:
        """Report new evidence and what is holding the entry back, throttled so it stays readable."""
        seen = set(computed.get("seen") or [])
        fresh = [code for code in verdict.codes if code not in seen]
        due = now - float(computed.get("progress_at") or 0.0) >= PROGRESS_THROTTLE_S
        if not fresh and not (due and verdict.confirmed):
            return
        computed["seen"] = sorted(seen | set(verdict.codes))
        computed["progress_at"] = now
        payload = {
            **base,
            "signals": [(s.code, s.detail) for s in verdict.signals],
            "core": verdict.core,
            "strength": verdict.strength,
            "strength_text": verdict.strength_text,
            "score": verdict.score,
            "score_min": SCORE_MIN,
            "holds": verdict.hold_texts(),
        }
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE setups SET computed_json = ?, score = ? WHERE id = ?",
                (json.dumps(computed, default=str), verdict.score, setup_id),
            )
            self.store.add_event(conn, setup_id, now, "EVIDENCE", payload)
            key = f"evidence:{int(now // PROGRESS_THROTTLE_S)}"
            self._queue(conn, setup_id, key, tg.evidence_message(payload), now)

    # ---------------------------------------------------------------- decision
    def _decide(
        self,
        setup: Setup,
        setup_id: str,
        computed: dict[str, Any],
        ctx: MarketContext,
        verdict: Verdict,
        price: float,
        touch_ts: float,
        now: float,
    ) -> None:
        """ENTER NOW while price is still in a fair place, otherwise a recomputed LIMIT."""
        decimals = self._decimals(setup.symbol)
        bars = window(ctx, touch_ts)
        reason = ""
        if verdict.late:
            entry, entry_why = planning.limit_price(setup, bars, price, ctx)
            mode, reason = "LIMIT", "late"
        elif planning.in_premium_half(setup, price):
            # Confirmed, but at the expensive edge of the zone: wait for the better half instead of
            # paying the top of it. A worse fill is a wider stop and a smaller R on the same idea.
            entry, entry_why = planning.discount_entry(setup, bars, price, ctx)
            mode, reason = "LIMIT", "premium"
        else:
            entry, entry_why = price, "çmimi live"
            mode = "MARKET"
        built = planning.build_plan(setup, mode, entry, verdict.extreme, ctx, notes=list(setup.notes))
        computed["plan"] = built.as_dict()
        computed["entry_why"] = entry_why
        payload = {
            "symbol": setup.symbol,
            "direction": setup.direction,
            "setup_id": setup_id,
            **built.as_dict(),
            "entry_why": entry_why,
            "reason": reason,
            "advance_r": verdict.advance_r,
            "signals": [(s.code, s.detail) for s in verdict.signals],
            "core": verdict.core,
            "strength": verdict.strength,
            "strength_text": verdict.strength_text,
            "score": verdict.score,
            "bars": verdict.bars,
            "spread": ctx.spread,
            "touch_ny": ny_string(touch_ts)[-8:-3],
        }
        text = tg.limit_message(payload, decimals) if mode == "LIMIT" else tg.enter_message(payload, decimals)
        state = LIMIT if mode == "LIMIT" else ENTERED
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE setups SET state = ?, computed_json = ?, score = ?, entry_price = ?, "
                "triggered_at = ? WHERE id = ?",
                (
                    state,
                    json.dumps(computed, default=str),
                    verdict.score,
                    built.entry,
                    now if state == ENTERED else None,
                    setup_id,
                ),
            )
            self.store.add_event(conn, setup_id, now, mode, payload)
            self._queue(conn, setup_id, mode.lower(), text, now)

    def _await_fill(
        self, setup: Setup, setup_id: str, computed: dict[str, Any], ctx: MarketContext, price: float, now: float
    ) -> None:
        built = planning.Plan.from_dict(computed["plan"])
        reached = price <= built.entry if setup.is_long else price >= built.entry
        if not reached:
            return
        payload = {"symbol": setup.symbol, "direction": setup.direction, "setup_id": setup_id, **built.as_dict()}
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE setups SET state = ?, triggered_at = ?, entry_price = ? WHERE id = ?",
                (ENTERED, now, built.entry, setup_id),
            )
            self.store.add_event(conn, setup_id, now, "FILLED", payload)
            self._queue(conn, setup_id, "filled", tg.filled_message(payload, self._decimals(setup.symbol)), now)

    # ------------------------------------------------------------- management
    def _manage(
        self, setup: Setup, setup_id: str, computed: dict[str, Any], ctx: MarketContext, price: float, now: float
    ) -> None:
        """After entry the only job is protecting the profit (docs/VALIDATOR.md §5)."""
        built = planning.Plan.from_dict(computed["plan"])
        decimals = self._decimals(setup.symbol)
        base = {"symbol": setup.symbol, "direction": setup.direction, "setup_id": setup_id, "price": price}
        risk = built.risk or setup.risk

        if (price <= built.stop) if setup.is_long else (price >= built.stop):
            self._close(setup_id, "SL", now, base, tg.sl_message(base, decimals))
            return

        moved = (price - built.entry) if setup.is_long else (built.entry - price)
        done = list(computed.get("tps_done") or [])
        for index, target in enumerate(built.targets, start=1):
            if index in done:
                continue
            if (price >= target) if setup.is_long else (price <= target):
                done.append(index)
                computed["tps_done"] = done
                payload = {**base, "n": index, "r_multiple": moved / risk if risk else 0.0}
                last = index >= len(built.targets)
                self._emit(setup_id, computed, f"TP{index}", payload, tg.tp_message(payload, decimals), now)
                if last:
                    self._close(setup_id, f"TP{index}", now, payload, "")
                return

        if not computed.get("secure_done") and (
            (price >= built.secure_at) if setup.is_long else (price <= built.secure_at)
        ):
            computed["secure_done"] = True
            payload = {
                **base,
                "secure_at": built.secure_at,
                "secure_why": built.secure_why,
                "secure_r": moved / risk if risk else 0.0,
            }
            self._emit(setup_id, computed, "SECURE", payload, tg.secure_message(payload, decimals), now)
            return

        if not computed.get("be_done") and risk and moved >= BE_R * risk:
            computed["be_done"] = True
            self._emit(setup_id, computed, "BREAKEVEN", base, tg.breakeven_message(base, decimals), now)
            return

        if not computed.get("reversal_done") and self._reversal(setup, ctx, built):
            computed["reversal_done"] = True
            self._emit(setup_id, computed, "REVERSAL", base, tg.reversal_message(base, decimals), now)

    @staticmethod
    def _reversal(setup: Setup, ctx: MarketContext, built: planning.Plan) -> bool:
        """An opposing micro market-structure break after entry and before TP1."""
        bars = ctx.candles("M1")[-20:]
        if len(bars) < 6:
            return False
        indices = swing_low_indices(bars) if setup.is_long else swing_high_indices(bars)
        if not indices:
            return False
        level = bars[indices[-1]].l if setup.is_long else bars[indices[-1]].h
        for bar in bars[indices[-1] + 1 :]:
            broke = bar.c < level if setup.is_long else bar.c > level
            if broke:
                return True
        return False

    # ------------------------------------------------------------------ writes
    def _emit(
        self, setup_id: str, computed: dict[str, Any], kind: str, payload: dict[str, Any], text: str, now: float
    ) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE setups SET computed_json = ?, last_processed_at = ? WHERE id = ?",
                (json.dumps(computed, default=str), now, setup_id),
            )
            self.store.add_event(conn, setup_id, now, kind, payload)
            if text:
                self._queue(conn, setup_id, kind.lower(), text, now)

    def _close(self, setup_id: str, outcome: str, now: float, payload: dict[str, Any], text: str) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE setups SET state = ?, closed_at = ?, outcome = ? WHERE id = ?",
                (DONE, now, outcome, setup_id),
            )
            self.store.add_event(conn, setup_id, now, outcome, payload)
            if text:
                self._queue(conn, setup_id, outcome.lower(), text, now)

    # ------------------------------------------------------------------ owner
    def cancel(self, setup_id: str) -> dict[str, Any]:
        row = self.store.get_setup(setup_id)
        if row is None:
            return {"status": "not_found"}
        if row["state"] == DONE:
            return {"status": "already_closed"}
        now = self.clock()
        self._close(setup_id, "MANUAL", now, {"setup_id": setup_id}, tg.manual_cancel_message(setup_id))
        return {"status": "cancelled"}

    def status(self, setup_id: str | None = None) -> dict[str, Any]:
        if setup_id:
            row = self.store.get_setup(setup_id)
            if row is None:
                return {"status": "not_found"}
            return self._summary(row, detailed=True)
        active = [self._summary(row) for row in self.store.setups_in_state(OPEN_STATES)]
        closed = [self._summary(row) for row in self.store.recent_setups(8) if row["state"] == DONE]
        return {
            "active": active,
            "recent_closed": closed,
            "paused": self.paused(),
            "notifications": self.store.outbox_health(),
        }

    def _summary(self, row, detailed: bool = False) -> dict[str, Any]:
        try:
            setup, computed = self._decode(row)
        except (KeyError, TypeError, ValueError):
            # A setup written by the previous, strategy-bound validator. It is never processed
            # again, but asking for it must not break the answer.
            return {
                "setup_id": row["id"],
                "symbol": row["symbol"],
                "direction": row["direction"],
                "state": row["state"],
                "legacy": True,
                "created_ny": ny_string(row["created_at"]),
            }
        out = {
            "setup_id": row["id"],
            "symbol": row["symbol"],
            "direction": row["direction"],
            "state": row["state"],
            "zone": [setup.zone_low, setup.zone_high],
            "stop_loss": row["entry_price"] and computed.get("plan", {}).get("stop") or setup.stop_loss,
            "targets": setup.targets,
            "outcome": row["outcome"],
            "created_ny": ny_string(row["created_at"]),
        }
        if detailed:
            out["notes"] = setup.notes
            out["plan"] = computed.get("plan")
            out["evidence"] = computed.get("seen") or []
            out["events"] = [
                {"ts": ny_string(event["ts"]), "type": event["type"]} for event in self.store.events_for(row["id"], 12)
            ]
            # A state change nobody was told about is a bug that looks exactly like no state change.
            out["notifications"] = self.store.messages_for(row["id"])
        return out

    def stats(self, days: int = 7) -> dict[str, Any]:
        since = self.clock() - days * 86400
        rows = self.store.query("SELECT * FROM setups WHERE created_at >= ?", (since,))
        outcomes = [row["outcome"] or "" for row in rows]
        return {
            "n": len(rows),
            "ent": sum(1 for row in rows if row["triggered_at"]),
            "cancel": sum(1 for o in outcomes if o.startswith("CANCELLED")),
            "win": sum(1 for o in outcomes if o.startswith("TP")),
            "loss": sum(1 for o in outcomes if o == "SL"),
            "open": sum(1 for row in rows if row["state"] in OPEN_STATES),
        }
