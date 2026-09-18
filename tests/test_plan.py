"""Recalculated entry, stop and the profit-securing level (docs/VALIDATOR.md §4 and §5)."""

from __future__ import annotations

import pytest

from app.plan import (
    MIN_R_FRACTION,
    build_plan,
    clamp_stop,
    limit_price,
    round_levels,
    secure_level,
    stop_buffer,
    structural_stop,
)
from app.setup_model import normalise
from tests.synth import Tape

LONG_RAW = {"entry_low": 4295.0, "entry_high": 4300.0, "stop_loss": 4280.0, "tp1": 4330.0}


def tape_with_history() -> Tape:
    tape = Tape(price=4290.0)
    tape.drift(60, step=0.1, span=0.6)
    return tape


def test_the_stop_sits_a_buffer_beyond_the_structural_extreme():
    ctx = tape_with_history().context()
    setup = normalise(LONG_RAW)
    buffer = stop_buffer(ctx)
    assert buffer > 0
    assert structural_stop(setup, 4291.0, ctx) == pytest.approx(4291.0 - buffer)


def test_the_new_stop_is_never_wider_than_the_one_the_setup_gave():
    setup = normalise(LONG_RAW)
    stop, notes = clamp_stop(setup, entry=4298.0, candidate=4260.0)
    assert stop == setup.stop_loss
    assert any("më i gjerë" in note for note in notes)


def test_the_new_stop_is_never_tighter_than_a_third_of_the_original_risk():
    setup = normalise(LONG_RAW)
    stop, notes = clamp_stop(setup, entry=4298.0, candidate=4297.9)
    assert stop == pytest.approx(4298.0 - MIN_R_FRACTION * setup.risk)
    assert any("i ngushtë" in note for note in notes)


def test_a_tighter_structural_stop_inside_the_bounds_is_used():
    setup = normalise(LONG_RAW)
    stop, notes = clamp_stop(setup, entry=4298.0, candidate=4290.0)
    assert stop == 4290.0
    assert notes == []


def test_the_same_clamps_hold_for_a_short():
    setup = normalise({"entry_low": 4300.0, "entry_high": 4305.0, "stop_loss": 4320.0, "tp1": 4270.0})
    assert clamp_stop(setup, entry=4302.0, candidate=4340.0)[0] == setup.stop_loss
    assert clamp_stop(setup, entry=4302.0, candidate=4302.1)[0] == pytest.approx(
        4302.0 + MIN_R_FRACTION * setup.risk
    )


# ------------------------------------------------------------------- securing
def test_round_numbers_are_symbol_specific():
    assert round_levels("XAUUSD", 4298.0, 4312.0) == [4300.0, 4305.0, 4310.0]
    assert round_levels("BTCUSD", 76000.0, 76600.0) == [76250.0, 76500.0]
    assert round_levels("EURUSD", 1.0, 2.0) == []


def test_the_secure_level_is_the_nearest_obstacle_and_never_beyond_one_r():
    ctx = tape_with_history().context()
    setup = normalise(LONG_RAW)
    level, why = secure_level(setup, entry=4298.0, stop=4290.0, ctx=ctx)
    risk = 4298.0 - 4290.0
    assert 4298.0 + 0.5 * risk <= level <= 4298.0 + risk
    assert why


def test_without_any_obstacle_the_secure_level_falls_back_to_one_r():
    tape = Tape(price=4290.0)
    tape.drift(60, step=0.0, span=0.05)  # a flat tape: no swings worth naming
    # A risk so small that no swing, session level or round number falls inside the window.
    setup = normalise({"entry": 8888.0, "stop_loss": 8887.5, "tp1": 8920.0})
    level, why = secure_level(setup, entry=8888.0, stop=8887.5, ctx=tape.context())
    assert level == pytest.approx(8888.5)
    assert why == "1R"


# ---------------------------------------------------------------- limit entry
def test_the_limit_price_sits_between_the_runaway_price_and_the_zone():
    tape = tape_with_history()
    setup = normalise(LONG_RAW)
    tape.push(4300.0, 4306.0, 4299.0, 4305.0)
    price = 4308.0
    level, why = limit_price(setup, tape.store().series("XAUUSD", "M1")[-3:], price, tape.context())
    assert setup.entry_edge <= level < price
    assert why


def test_a_plan_keeps_the_targets_and_recomputes_their_r_multiples():
    ctx = tape_with_history().context()
    setup = normalise({**LONG_RAW, "tp2": 4360.0})
    built = build_plan(setup, "MARKET", entry=4299.0, extreme=4291.0, ctx=ctx)
    assert built.targets == [4330.0, 4360.0]
    assert built.rr[0] == pytest.approx((4330.0 - 4299.0) / built.risk, abs=0.01)
    assert built.risk > 0
    assert built.secure_at != built.entry


def test_a_target_already_passed_is_dropped_from_the_plan():
    ctx = tape_with_history().context()
    setup = normalise({**LONG_RAW, "tp2": 4360.0})
    built = build_plan(setup, "MARKET", entry=4335.0, extreme=4325.0, ctx=ctx)
    assert built.targets == [4360.0]
