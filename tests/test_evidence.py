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
    atr = tape.context().atr("M1") or 1.0
    tape.push(4298.0, 4298.0 + 2 * atr, 4297.8, 4298.0 + 1.8 * atr)  # strong body in the trade direction
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
    tape.drift(1, step=1.5)
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
