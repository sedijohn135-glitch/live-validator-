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


def break_supply(tape: Tape) -> None:
    """A swing high, a pullback off it, then a close through it: the nearest supply gives way.

    No entry exists without this, so a tape that is meant to confirm has to contain it.
    """
    atr = tape.context().atr("M1") or 1.0
    o = tape.price
    peak = o + 1.2 * atr
    tape.push(o, peak, o - 0.1, o + 0.9 * atr)  # the swing high
    dip = peak - 1.4 * atr
    tape.push(o + 0.9 * atr, peak - 0.1 * atr, dip, dip + 0.1 * atr)  # the pullback
    tape.push(dip + 0.1 * atr, peak + 0.8 * atr, dip, peak + 0.6 * atr)  # the close through it


def verdict_for(tape: Tape, setup, touch_ts: float, **ctx_kwargs):
    return evaluate(setup, tape.context(**ctx_kwargs), touch_ts)


def test_a_sweep_and_reclaim_is_supporting_evidence_not_a_confirmation():
    """Real evidence, and on its own still not an entry: it is none of the owner's three."""
    tape = approach()
    touch = tape.now
    tape.drift(1, step=-1.0)
    tape.sweep(low=4291.0, close=4298.0)  # liquidity taken below the zone, price back inside
    verdict = verdict_for(tape, long_setup(), touch)
    assert "RECLAIM" in verdict.codes
    assert verdict.core == [], "a reclaim is not one of the three"
    assert not verdict.confirmed


def test_a_rejection_wick_is_a_primary_signal():
    tape = approach()
    touch = tape.now
    tape.push(4299.0, 4301.5, 4296.0, 4301.0)  # long lower wick into the zone, close back above
    verdict = verdict_for(tape, long_setup(), touch)
    assert "REJECTION" in verdict.codes


def test_the_nearest_supply_broken_is_a_confirmation():
    tape = approach()
    touch = tape.now
    tape.drift(3, step=-0.3)
    tape.push(4295.0, 4297.0, 4294.5, 4296.5)  # a micro swing high forms
    tape.drift(2, step=-0.4)
    tape.push(4295.0, 4299.0, 4294.8, 4298.5)  # and is closed through
    verdict = verdict_for(tape, long_setup(), touch)
    assert "ZONE_BREAK" in verdict.codes


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
    break_supply(tape)
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
    assert verdict.codes, "the evidence is still evidence — only the entry waits"


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
    break_supply(tape)
    ready = verdict_for(tape, long_setup(), touch)
    assert ready.ready

    stalled = verdict_for(tape, long_setup(), touch, bid=4291.2)  # back on the extreme
    assert "REACTION" in stalled.holds
    assert not stalled.ready


def test_one_close_beyond_the_head_invalidates_the_zone():
    """Step 6: një qiri që mbyllet jashtë zonës së QM e anulon setupin. One candle, not two."""
    from app.evidence import zone_failed

    tape = approach()
    touch = tape.now
    setup = long_setup()
    assert not zone_failed(setup, tape.context(), touch)

    atr = tape.context().atr("M1") or 1.0
    tape.push(setup.zone_low, setup.zone_low, setup.zone_low - 2 * atr, setup.zone_low - 1.5 * atr)
    assert zone_failed(setup, tape.context(), touch), "one close beyond the head is the rule"


def test_a_close_just_past_the_edge_is_not_a_break():
    """The margin exists so a tick past the edge is not mistaken for a candle closing outside."""
    from app.evidence import zone_failed

    tape = approach()
    touch = tape.now
    setup = long_setup()
    tape.push(setup.zone_low, setup.zone_low, setup.zone_low - 0.02, setup.zone_low - 0.01)
    assert not zone_failed(setup, tape.context(), touch)


def test_an_ao_divergence_alone_is_never_an_entry():
    """Step 2 is explicit: divergjenca NUK është sinjal hyrjeje, por paralajmërim.

    It raises the strength of an entry the break has already made. It never makes one.
    """
    tape = Tape(price=4300.0)
    tape.drift(40, step=0.25, span=0.4)
    touch = tape.now
    tape.drift(10, step=-0.10, span=0.3)
    _bar(tape, tape.price, tape.price + 1.2, tape.price - 0.2, tape.price + 0.4)
    tape.drift(3, step=-0.25, span=0.2)
    top = tape.price + 2.0
    _bar(tape, tape.price, top, tape.price - 0.2, top - 0.4)
    tape.drift(2, step=-0.2, span=0.2)
    setup = normalise({"entry_low": top - 1.0, "entry_high": top + 1.0, "stop_loss": top + 5.0, "tp1": top - 20.0})
    verdict = verdict_for(tape, setup, touch)
    assert "AO_DIV" in verdict.codes, verdict.codes
    assert "ZONE_BREAK" not in verdict.codes
    assert "BREAK" in verdict.holds
    assert not verdict.confirmed, "a warning is not an entry"


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


def test_the_failed_setup_never_reaches_an_entry_at_all(tmp_path):
    """The honest reading of that window, end to end through the engine.

    At 14:10 the evidence was real — a 0.60 dip under the zone low is half an ATR at that
    volatility, and the better half of the zone then held for three bars. Two things were still
    wrong. The fill: 4392.20 is the expensive edge of a 4389.82-4392.35 demand zone. And the
    structure: the nearest supply above was never broken in that window, so under the owner's rule
    no entry existed to place at any price. The state machine stays at the zone, waiting.
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
    assert row["state"] == "AT_ZONE", "none of the three confirmations appeared"
    assert row["triggered_at"] is None, "nothing was entered at any price"
    setup, computed = feed.engine.load_setup(setup_id)
    verdict = evaluate(setup, feed.context(feed.tape.symbol, feed.tape.now), float(computed["touch_ts"]))
    assert verdict.core == [], verdict.codes
    assert not verdict.confirmed


def test_the_same_window_holds_once_price_falls_back_to_the_edge():
    """And when price gave it all back, nothing was confirmed at all."""
    tape, touch = incident_tape()
    verdict = verdict_for(tape, incident_setup(), touch, bid=4389.76)
    assert "REACTION" in verdict.holds
    assert not verdict.ready


# ------------------------------------------------------- quasimodo, both directions
def _bar(tape, o, h, low, c):
    tape.push(o, h, low, c)


def test_a_sell_quasimodo_is_a_primary_signal():
    """Left shoulder, a higher head, the neckline broken, then price back at the shoulder.

    The owner's diagram: the entry lives at the right shoulder, which sits at the left shoulder's
    level. The head is what took the liquidity above it.
    """
    tape = Tape(price=4300.0)
    tape.drift(40, step=0.02, span=0.4)
    touch = tape.now
    _bar(tape, 4300.0, 4305.0, 4299.5, 4304.0)  # left shoulder high 4305
    _bar(tape, 4304.0, 4304.2, 4300.0, 4300.5)  # the neckline low 4300
    _bar(tape, 4300.5, 4308.0, 4300.4, 4307.0)  # the head, higher than the shoulder
    _bar(tape, 4307.0, 4307.2, 4302.0, 4302.5)
    _bar(tape, 4302.5, 4302.6, 4297.0, 4297.5)  # closes through the neckline
    _bar(tape, 4297.5, 4305.2, 4297.4, 4304.8)  # back at the shoulder: the right shoulder
    setup = normalise({"entry_low": 4304.0, "entry_high": 4306.0, "stop_loss": 4310.0, "tp1": 4290.0})
    assert "QUASIMODO" in verdict_for(tape, setup, touch).codes


def test_a_buy_quasimodo_is_the_mirror():
    tape = Tape(price=4300.0)
    tape.drift(40, step=-0.02, span=0.4)
    touch = tape.now
    _bar(tape, 4300.0, 4300.5, 4295.0, 4296.0)  # left shoulder low 4295
    _bar(tape, 4296.0, 4300.0, 4295.8, 4299.5)  # the neckline high 4300
    _bar(tape, 4299.5, 4299.6, 4292.0, 4293.0)  # the head, lower than the shoulder
    _bar(tape, 4293.0, 4298.0, 4292.8, 4297.5)
    _bar(tape, 4297.5, 4303.0, 4297.4, 4302.5)  # closes through the neckline
    _bar(tape, 4302.5, 4302.6, 4294.8, 4295.2)  # back at the shoulder
    setup = normalise({"entry_low": 4294.0, "entry_high": 4296.0, "stop_loss": 4290.0, "tp1": 4310.0})
    assert "QUASIMODO" in verdict_for(tape, setup, touch).codes


def test_a_return_to_the_shoulder_before_the_neckline_breaks_is_not_a_quasimodo():
    """The pattern is still forming. The break is what says who won, and it has not happened."""
    tape = Tape(price=4300.0)
    tape.drift(40, step=0.02, span=0.4)
    touch = tape.now
    _bar(tape, 4300.0, 4305.0, 4299.5, 4304.0)
    _bar(tape, 4304.0, 4304.2, 4300.0, 4300.5)
    _bar(tape, 4300.5, 4308.0, 4300.4, 4307.0)
    _bar(tape, 4307.0, 4305.1, 4302.0, 4302.5)  # back at the shoulder, neckline still intact
    setup = normalise({"entry_low": 4304.0, "entry_high": 4306.0, "stop_loss": 4310.0, "tp1": 4290.0})
    assert "QUASIMODO" not in verdict_for(tape, setup, touch).codes


def test_an_ao_divergence_is_a_primary_signal():
    """Price made a higher high, the oscillator did not: the push had nothing behind it.

    The owner's screenshot: XAUUSD M1, price pressing to a new high while AO rolled over. This is
    the confirmation that arrives when the expected sweep-and-reclaim never does.
    """
    tape = Tape(price=4300.0)
    tape.drift(40, step=0.25, span=0.4)  # a strong leg up: AO builds
    touch = tape.now
    tape.drift(10, step=-0.10, span=0.3)  # the leg fades: AO rolls over
    _bar(tape, tape.price, tape.price + 1.2, tape.price - 0.2, tape.price + 0.4)  # swing high A
    tape.drift(3, step=-0.25, span=0.2)
    top = tape.price + 2.0
    _bar(tape, tape.price, top, tape.price - 0.2, top - 0.4)  # swing high B, higher than A
    tape.drift(2, step=-0.2, span=0.2)
    setup = normalise({"entry_low": top - 1.0, "entry_high": top + 1.0, "stop_loss": top + 5.0, "tp1": top - 20.0})
    verdict = verdict_for(tape, setup, touch)
    assert "AO_DIV" in verdict.codes, verdict.codes


def test_any_one_of_the_three_is_an_entry_and_each_extra_one_is_stronger():
    """The owner's rule: 1 is an entry, 2 is stronger, 3 is as strong as this engine reads.

    No confirmation is mandatory and none outranks another. A sweep and an impulse are real
    evidence, but neither is one of the three, so on their own they are not an entry. The moment
    the nearest supply gives way, it is.
    """
    tape = approach()
    touch = tape.now
    tape.sweep(low=4291.0, close=4298.0)
    impulse(tape)
    before = verdict_for(tape, long_setup(), touch)
    assert before.codes, "the supporting evidence is there"
    assert before.strength == 0
    assert not before.ready, "and it is still not an entry"

    break_supply(tape)
    after = verdict_for(tape, long_setup(), touch)
    assert after.core == ["ZONE_BREAK"]
    assert after.strength == 1
    assert after.strength_text == "konfirmim"
    assert after.ready, "one of the three is enough"
