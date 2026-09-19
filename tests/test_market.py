import pytest

from app.market import (
    Candle,
    CandleStore,
    atr14,
    efficiency_ratio,
    find_fvgs,
    fvg_status,
    median_spread,
    session_levels,
    swing_high_indices,
    swing_low_indices,
    tolerance,
    true_range,
)
from tests.helpers import flat_series, ts


def c(t, o, h, low, close):
    return Candle(t, o, h, low, close)


def test_body_helpers():
    bull = c(0, 10, 12, 9, 11)
    assert bull.body_top == 11 and bull.body_bot == 10
    assert bull.body == 1 and bull.span == 3
    assert bull.bullish and not bull.bearish


def test_true_range_uses_previous_close():
    prev = c(0, 10, 11, 9, 10)
    cur = c(60, 14, 15, 13, 14)
    assert true_range(cur, prev) == pytest.approx(5.0)
    assert true_range(cur, None) == pytest.approx(2.0)


def test_atr14_needs_15_candles():
    candles = [c(i * 60, 10, 11, 9, 10) for i in range(14)]
    assert atr14(candles) is None
    candles.append(c(14 * 60, 10, 11, 9, 10))
    assert atr14(candles) == pytest.approx(2.0)


def test_strict_swings():
    candles = [c(0, 1, 5, 0, 2), c(60, 1, 7, 1, 2), c(120, 1, 4, 0, 2)]
    assert swing_high_indices(candles) == [1]
    candles = [c(0, 1, 5, 3, 2), c(60, 1, 5, 1, 2), c(120, 1, 5, 2, 2)]
    assert swing_low_indices(candles) == [1]


def test_equal_high_is_not_a_strict_swing():
    candles = [c(0, 1, 5, 0, 2), c(60, 1, 5, 1, 2), c(120, 1, 4, 0, 2)]
    assert swing_high_indices(candles) == []


def test_wick_fvg_bullish():
    candles = [c(0, 10, 11, 9, 10), c(60, 11, 15, 11, 14), c(120, 14, 16, 12, 15)]
    fvgs = find_fvgs(candles)
    assert len(fvgs) == 1
    fvg = fvgs[0]
    assert fvg.direction == "BULL" and fvg.kind == "wick"
    assert (fvg.low, fvg.high) == (11, 12)
    assert fvg.ce == 11.5
    assert fvg.formed_at == 60


def test_body_fvg_when_wicks_overlap():
    candles = [c(0, 10, 12, 9, 10.5), c(60, 11, 15, 11, 14), c(120, 13, 16, 11.5, 15)]
    fvgs = [f for f in find_fvgs(candles) if f.direction == "BULL"]
    assert fvgs and fvgs[0].kind == "body"
    assert (fvgs[0].low, fvgs[0].high) == (10.5, 13)


def test_bearish_wick_fvg():
    candles = [c(0, 14, 15, 13, 14), c(60, 13, 13, 9, 10), c(120, 10, 12, 8, 9)]
    fvgs = [f for f in find_fvgs(candles) if f.direction == "BEAR"]
    assert fvgs and (fvgs[0].low, fvgs[0].high) == (12, 13)


def test_fvg_status_progression():
    base = [c(0, 10, 11, 9, 10), c(60, 11, 15, 11, 14), c(120, 14, 16, 12, 15)]
    fvg = find_fvgs(base)[0]  # bullish [11, 12], CE 11.5
    assert fvg_status(fvg, base) == "untouched"
    tapped = base + [c(180, 15, 15, 11.9, 14)]
    assert fvg_status(fvg, tapped) == "tapped"
    breached = base + [c(180, 15, 15, 11.2, 14)]
    assert fvg_status(fvg, breached) == "ce_breached"
    failed = base + [c(180, 15, 15, 10, 10.5)]
    assert fvg_status(fvg, failed) == "failed"
    inverted = failed + [c(240, 10.5, 11.5, 10.4, 11.4)]
    assert fvg_status(fvg, inverted) == "inverted"


def test_efficiency_ratio_detects_chop():
    trend = [c(i * 60, i, i + 1, i - 1, i + 1) for i in range(30)]
    assert efficiency_ratio(trend) > 0.9
    chop = [c(i * 60, 10, 11, 9, 10 + (i % 2)) for i in range(30)]
    assert efficiency_ratio(chop) < 0.25


def test_tolerance_takes_the_largest_component():
    assert tolerance(10.0, 0.1, 0.01, 0.15) == pytest.approx(1.5)
    assert tolerance(0.1, 1.0, 0.01, 0.15) == pytest.approx(2.0)
    assert tolerance(None, 0.0, 0.5, 0.15) == pytest.approx(1.5)


def test_median_spread_falls_back_below_30_samples():
    assert median_spread([0.5] * 10, 0.9) == pytest.approx(0.3)
    assert median_spread([0.5] * 40, 0.9) == pytest.approx(0.5)


def test_store_drops_forming_candles():
    store = CandleStore(close_grace_s=2.0)
    now = 1000.0
    added = store.merge("XAUUSD", "M1", [Candle(880, 1, 2, 0, 1), Candle(940, 1, 2, 0, 1)], now)
    assert added == 1  # the 940 bar closes at 1000, grace not met
    assert store.last("XAUUSD", "M1").t == 880


def test_store_merges_by_open_timestamp():
    store = CandleStore(close_grace_s=0.0)
    now = 10_000.0
    store.merge("XAUUSD", "M1", [Candle(600, 1, 2, 0, 1)], now)
    store.merge("XAUUSD", "M1", [Candle(600, 1, 5, 0, 4)], now)
    assert len(store.series("XAUUSD", "M1")) == 1
    assert store.last("XAUUSD", "M1").h == 5


def test_store_reports_missing_bars():
    store = CandleStore(close_grace_s=0.0)
    now = 10_000.0
    store.merge("XAUUSD", "M1", [Candle(600, 1, 2, 0, 1), Candle(720, 1, 2, 0, 1)], now)
    assert store.missing_between("XAUUSD", "M1", 600, 720) == 1


def test_session_levels():
    store = CandleStore(close_grace_s=0.0)
    now = ts("2026-09-16 08:00")
    store.merge("XAUUSD", "M5", flat_series("M5", 400, now, 5650.0), now)
    store.merge("XAUUSD", "D1", flat_series("D1", 40, now, 5600.0), now)
    store.merge("XAUUSD", "W1", flat_series("W1", 8, now, 5500.0), now)
    levels = session_levels(store, "XAUUSD", now)
    assert levels["asian_high"] > levels["asian_low"]
    assert levels["ny_midnight_open"] is not None
    assert levels["six_am_open"] is not None
    assert levels["pdh"] is not None and levels["pwh"] is not None
    assert levels["lookback_high"] >= levels["pdh"]


# ------------------------------------------------- the oscillator, computed for the model
def test_the_awesome_oscillator_matches_its_definition():
    """SMA5 - SMA34 of the median price, and nothing clever. Checked against a hand computation."""
    from app.market import AO_SLOW, awesome_oscillator

    bars = [Candle(float(i), 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i) for i in range(40)]
    values = awesome_oscillator(bars)
    assert len(values) == len(bars) - AO_SLOW + 1

    medians = [(bar.h + bar.l) / 2 for bar in bars]
    assert values[-1] == pytest.approx(sum(medians[-5:]) / 5 - sum(medians[-34:]) / 34)


def test_a_sell_divergence_is_named_from_the_candles_alone():
    """Price makes a higher high, the oscillator does not follow.

    The model reads this from the snapshot. It never works out a 34-period mean in prose, which is
    the one part of this strategy an LLM cannot be trusted with.
    """
    from app.market import ao_divergence

    bars = [Candle(float(i), 100.0 + i * 0.5, 101.0 + i * 0.5, 99.0 + i * 0.5, 100.5 + i * 0.5) for i in range(36)]
    base = bars[-1].c
    bars += [
        Candle(36.0, base, base + 4.0, base - 0.5, base + 3.0),  # the first swing high
        Candle(37.0, base + 3.0, base + 3.2, base - 6.0, base - 5.0),  # a drop that drains the AO
        Candle(38.0, base - 5.0, base - 4.0, base - 8.0, base - 7.0),
        Candle(39.0, base - 7.0, base + 4.5, base - 7.2, base + 1.0),  # a higher high, less force
        Candle(40.0, base + 1.0, base + 1.2, base - 2.0, base - 1.5),
    ]
    side, detail = ao_divergence(bars)
    assert side == "SELL", (side, detail)
    assert "AO më poshtë" in detail
