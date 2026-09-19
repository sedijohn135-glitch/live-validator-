"""Each live evidence signal, the balance that makes a verdict, and the holds (docs/VALIDATOR.md §2)."""

from __future__ import annotations

import pytest

from app.evidence import SCORE_MIN, evaluate
from app.setup_model import normalise
from tests.synth import Tape

ZONE = {"entry_low": 4295.0, "entry_high": 4300.0, "stop_loss": 4288.0, "tp1": 4330.0}
SHORT_ZONE = {"entry_low": 4300.0, "entry_high": 4305.0, "stop_loss": 4312.0, "tp1": 4270.0}


def long_setup(**over):
    return normalise({**ZONE, **over})


def approach(price: float = 4297.0, bars: int = 30) -> Tape:
    """A quiet walk down into the zone, so ATR is sane and the touch is the newest bar."""
    tape = Tape(price=price + 6.0)
    tape.drift(bars, step=-0.2, span=0.5)
    return tape


def impulse(tape: Tape) -> None:
    """A candle that is both big and closes at its extreme — a real impulse, not a wide range."""
    atr = tape.context().atr("M1") or 1.0
    o = tape.price
    tape.push(o, o + 2 * atr, o - 0.1, o + 1.8 * atr)


def verdict_for(tape: Tape, setup, touch_ts: float, **ctx_kwargs):
    return evaluate(setup, tape.context(**ctx_kwargs), touch_ts)


def test_a_sweep_and_reclaim_is_a_primary_signal():
    tape = approach()
    touch = tape.now
    tape.drift(1, step=-1.0)
    tape.sweep(low=4291.0, close=4298.0)  # liquidity taken below the zone, price back inside
    verdict = verdict_for(tape, long_setup(), touch)
    assert "RECLAIM" in verdict.codes
    assert verdict.has_primary


def test_a_rejection_wick_is_a_primary_signal():
    tape = approach()
    touch = tape.now
    tape.push(4299.0, 4301.5, 4296.0, 4301.0)  # long lower wick into the zone, close back above
    verdict = verdict_for(tape, long_setup(), touch)
    assert "REJECTION" in verdict.codes


def test_a_micro_structure_shift_is_a_primary_signal():
    tape = approach()
    touch = tape.now
    tape.drift(3, step=-0.3)
    tape.push(4295.0, 4297.0, 4294.5, 4296.5)  # a micro swing high forms
    tape.drift(2, step=-0.4)
    tape.push(4295.0, 4299.0, 4294.8, 4298.5)  # and is closed through
    verdict = verdict_for(tape, long_setup(), touch)
    assert "SHIFT" in verdict.codes


def test_momentum_and_absorption_alone_never_confirm():
    """Two weak signals are a reaction, not a confirmation (docs/VALIDATOR.md §2)."""
    tape = approach()
    touch = tape.now
    for _ in range(4):
        tape.push(4297.0, 4298.0, 4296.0, 4297.5)  # sitting at the zone, holding it
    verdict = verdict_for(tape, long_setup(), touch)
    assert "ABSORPTION" in verdict.codes
    assert not verdict.has_primary
    assert not verdict.confirmed


def test_one_primary_plus_one_weak_signal_confirms():
    tape = approach()
    touch = tape.now
    tape.sweep(low=4291.0, close=4298.0)
    impulse(tape)
    verdict = verdict_for(tape, long_setup(), touch)
    assert verdict.score >= SCORE_MIN
    assert verdict.has_primary
    assert verdict.ready


def test_the_same_signals_work_for_a_short():
    tape = Tape(price=4296.0)
    tape.drift(30, step=0.2, span=0.5)
    touch = tape.now
    tape.spike(high=4309.0, close=4302.0)  # sweep above the zone and close back inside
    verdict = verdict_for(tape, normalise(SHORT_ZONE), touch)
    assert "RECLAIM" in verdict.codes


# --------------------------------------------------------------------- holds
def test_a_wide_spread_holds_the_entry_without_killing_the_setup():
    tape = approach()
    touch = tape.now
    tape.sweep(low=4291.0, close=4298.0)
    impulse(tape)
    verdict = verdict_for(tape, long_setup(), touch, spread=9.0, median=0.2)
    assert "SPREAD" in verdict.holds
    assert not verdict.ready
    assert verdict.confirmed, "the evidence is still evidence — only the entry waits"


def test_stale_or_synthetic_prices_hold_the_entry():
    tape = approach()
    touch = tape.now
    tape.sweep(low=4291.0, close=4298.0)
    assert "DATA" in verdict_for(tape, long_setup(), touch, quote_ts=tape.now - 120).holds
    assert "DATA" in verdict_for(tape, long_setup(), touch, quote_synthetic=True).holds
    assert "DATA" in verdict_for(tape, long_setup(), touch, data_ok=False).holds


def test_a_falling_knife_holds_the_entry():
    tape = approach()
    touch = tape.now
    atr = tape.context().atr("M1") or 1.0
    for _ in range(3):
        tape.push(tape.price, tape.price, tape.price - 3 * atr, tape.price - 3 * atr)
    verdict = verdict_for(tape, long_setup(), touch)
    assert "KNIFE" in verdict.holds


def test_the_first_tick_into_the_zone_is_never_enough():
    tape = approach()
    verdict = verdict_for(tape, long_setup(), tape.now + 1)
    assert "FRESH" in verdict.holds
    assert not verdict.ready


# ---------------------------------------------------------------- not too late
def test_advance_tells_market_from_limit():
    setup = long_setup()
    tape = approach()
    touch = tape.now
    tape.sweep(low=4291.0, close=4298.0)
    near = verdict_for(tape, setup, touch, bid=4301.0)
    assert not near.late
    far = verdict_for(tape, setup, touch, bid=4306.0)
    assert far.late
    assert far.advance_r == pytest.approx((4306.0 - 4300.0) / setup.risk)


# ------------------------------------------- materiality: chop must not confirm itself
def test_a_one_tick_dip_below_the_zone_is_not_a_liquidity_sweep():
    """The defect the owner caught: a 1-cent dip counted as RECLAIM — 2 points, primary."""
    tape = approach()
    touch = tape.now
    setup = long_setup()
    tape.push(4295.10, 4295.40, setup.zone_low - 0.01, 4295.20)  # a tick under the edge, closes inside
    tape.drift(2, step=0.1, span=0.2)
    verdict = verdict_for(tape, setup, touch)
    assert "RECLAIM" not in verdict.codes
    assert not verdict.confirmed


def test_a_sweep_of_a_level_price_already_broke_takes_no_liquidity():
    """Dipping under the zone edge after price traded lower takes stops that are long gone."""
    tape = approach()
    touch = tape.now
    setup = long_setup()
    tape.push(4296.0, 4296.2, 4290.0, 4291.0)  # price breaks far below and stays there
    for close in (4291.5, 4292.0, 4292.5, 4293.0):
        tape.push(close - 0.2, close + 0.2, close - 0.4, close)
    tape.push(4293.0, 4296.2, 4292.9, 4296.00)  # back inside the zone, too late to be that reclaim
    tape.push(4296.0, 4296.4, setup.zone_low - 0.10, 4296.20)  # a dip under an edge with nothing left
    verdict = verdict_for(tape, setup, touch)
    assert "RECLAIM" not in verdict.codes


def test_a_doji_at_the_zone_is_not_a_rejection():
    tape = approach()
    touch = tape.now
    setup = long_setup()
    tape.push(setup.zone_high + 0.05, setup.zone_high + 0.30, setup.zone_high - 0.05, setup.zone_high + 0.25)
    verdict = verdict_for(tape, setup, touch)
    assert "REJECTION" not in verdict.codes


def test_a_two_tick_swing_is_not_a_structure_shift():
    tape = approach()
    touch = tape.now
    for _ in range(3):
        tape.push(4297.0, 4297.10, 4296.95, 4297.02)
    tape.push(4297.0, 4297.20, 4296.98, 4297.15)  # "breaks" a swing two cents tall
    verdict = verdict_for(tape, long_setup(), touch)
    assert "SHIFT" not in verdict.codes


def test_a_big_candle_that_gives_it_all_back_is_not_momentum():
    tape = approach()
    touch = tape.now
    atr = tape.context().atr("M1") or 1.0
    tape.push(4296.0, 4296.0 + 2.5 * atr, 4295.9, 4296.0 + 1.0 * atr)  # closes mid-range
    verdict = verdict_for(tape, long_setup(), touch)
    assert "MOMENTUM" not in verdict.codes


def test_drifting_in_the_lower_half_is_not_absorption():
    tape = approach()
    touch = tape.now
    setup = long_setup()
    for _ in range(5):
        tape.push(4296.2, 4296.6, 4295.9, 4296.10)  # below the zone mid the whole time
    verdict = verdict_for(tape, setup, touch)
    assert "ABSORPTION" not in verdict.codes


def test_without_a_reaction_off_the_extreme_nothing_is_confirmed():
    """Price sitting on the low it just made has defended nothing, whatever the patterns say."""
    tape = approach()
    touch = tape.now
    tape.sweep(low=4291.0, close=4298.0)
    impulse(tape)
    ready = verdict_for(tape, long_setup(), touch)
    assert ready.ready

    stalled = verdict_for(tape, long_setup(), touch, bid=4291.2)  # back on the extreme
    assert "REACTION" in stalled.holds
    assert not stalled.ready


def test_the_zone_breaking_is_not_the_same_as_the_zone_holding():
    from app.evidence import zone_failed

    tape = approach()
    touch = tape.now
    setup = long_setup()
    assert not zone_failed(setup, tape.context(), touch)

    atr = tape.context().atr("M1") or 1.0
    for _ in range(2):
        tape.push(setup.zone_low, setup.zone_low, setup.zone_low - 2 * atr, setup.zone_low - 1.5 * atr)
    assert zone_failed(setup, tape.context(), touch)


# ------------------------------------------------ the setup that failed on 2026-09-18
# XAU-0918-LYJ3: zone 4389.82-4392.35, stop 4383.80, registered 13:43 NY.
# Price entered the zone at 14:00, dipped to 4389.22 at 14:05, recovered to 4392.20 by 14:10, then
# sat back on the zone low until it broke down and took the stop at 14:42. The M5 bars below are the
# ones the feed actually returned; the M1 path is built to aggregate to them exactly.
M5_OBSERVED = [
    (4393.23, 4394.00, 4391.96, 4392.54),  # 14:00
    (4392.62, 4394.39, 4389.22, 4390.38),  # 14:05
    (4390.57, 4393.09, 4389.62, 4392.20),  # 14:10
]
M1_OBSERVED = [
    (4392.01, 4392.19, 4390.81, 4390.93),  # 14:16
    (4390.90, 4390.93, 4389.28, 4389.70),  # 14:17
    (4389.90, 4390.26, 4389.25, 4389.76),  # 14:18
]


def incident_tape() -> tuple[Tape, float]:
    """The real window, rebuilt bar by bar."""
    tape = Tape(start_ny="2026-09-18 13:20", price=4386.0)
    tape.drift(35, step=0.18, span=0.7)  # the run up into the zone, for a realistic ATR
    touch = tape.now
    for o, h, low, c in M5_OBSERVED:
        step = (c - o) / 5
        for i in range(5):
            bar_o = o + step * i
            bar_c = o + step * (i + 1)
            bar_h = h if i == 2 else max(bar_o, bar_c) + 0.05
            bar_l = low if i == 2 else min(bar_o, bar_c) - 0.05
            tape.push(bar_o, bar_h, bar_l, bar_c)
    for o, h, low, c in M1_OBSERVED:
        tape.push(o, h, low, c)
    return tape, touch


def incident_setup():
    return normalise({"entry_low": 4389.82, "entry_high": 4392.35, "stop_loss": 4383.80, "tp1": 4399.62})


def test_the_setup_that_failed_is_not_confirmed_where_it_sat():
    """The owner's objection: would this have been an ENTER? Where price actually sat, no."""
    tape, touch = incident_tape()
    verdict = verdict_for(tape, incident_setup(), touch, bid=4389.76)
    assert not verdict.ready, f"scored {verdict.score} on {verdict.codes}"
    assert "REACTION" in verdict.holds


def test_the_failed_setup_would_have_been_a_limit_not_a_market_fill(tmp_path):
    """The honest reading of that window, end to end through the engine.

    At 14:10 the evidence was real: a 0.60 dip under the zone low is half an ATR at the volatility
    of the moment, and the zone's better half then held for three bars. What was wrong was the fill —
    4392.20 is the expensive edge of a 4389.82-4392.35 demand zone. The validator now waits for the
    cheap half instead of paying the top of its own zone.
    """
    from tests.synth import Feed

    feed = Feed(tmp_path, start_ny="2026-09-18 13:20", price=4386.0)
    setup_id = feed.submit(entry_low=4389.82, entry_high=4392.35, stop_loss=4383.80, tp1=4399.62)["setup_id"]
    feed.tape.drift(35, step=0.18, span=0.7)
    feed.tick(price=4392.30)  # the touch, coming down into the zone
    assert feed.state(setup_id) == "AT_ZONE"

    for o, h, low, c in M5_OBSERVED:
        step = (c - o) / 5
        for i in range(5):
            bar_o, bar_c = o + step * i, o + step * (i + 1)
            feed.tape.push(
                bar_o,
                h if i == 2 else max(bar_o, bar_c) + 0.05,
                low if i == 2 else min(bar_o, bar_c) - 0.05,
                bar_c,
            )
    feed.tick(price=4392.20)  # the top of the recovery

    row = feed.store.get_setup(setup_id)
    assert row["state"] == "LIMIT", "a fill at the expensive edge of the zone is not an entry"
    import json

    plan = json.loads(row["computed_json"])["plan"]
    assert plan["entry"] <= 4391.09, "the order belongs in the cheap half of the zone"
    assert "gjysma e shtrenjtë" in feed.last_message()


def test_the_same_window_holds_once_price_falls_back_to_the_edge():
    """And when price gave it all back, nothing was confirmed at all."""
    tape, touch = incident_tape()
    verdict = verdict_for(tape, incident_setup(), touch, bid=4389.76)
    assert "REACTION" in verdict.holds
    assert not verdict.ready
