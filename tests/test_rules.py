"""One passing and one failing case per intake rule, plus the live and trigger rules."""

from __future__ import annotations

import pytest

from app import rules
from app.config import BALANCED, STRICT
from app.market import Candle
from app.rules import evaluate_intake, hard_level
from app.setup_model import normalise
from tests.test_golden import BASE_SETUP, clean_long_scenario, ts


@pytest.fixture()
def ctx():
    scenario = clean_long_scenario()
    return scenario.context_provider()("XAUUSD", ts("2026-09-16 07:46"))


def make(**overrides):
    return normalise(dict(BASE_SETUP, **overrides))


def codes(setup, ctx) -> list[str]:
    return [f.code for f in evaluate_intake(setup, ctx).failures]


def test_baseline_setup_passes_every_rule(ctx):
    result = evaluate_intake(make(), ctx)
    assert result.passed, [f.code for f in result.failures]
    assert result.computed["rr_plan"] > 2
    assert result.computed["expires_at_ny"] == "2026-09-16 09:00"
    assert "prekje" in result.computed["waits_for_sq"]


@pytest.mark.parametrize(
    ("code", "overrides"),
    [
        ("G-01", {"direction": "UP"}),
        ("G-01", {"entry_model": "NOT_A_MODEL"}),
        ("G-01", {"pda_formed_at_ny": "yesterday"}),
        ("G-01", {"tp1": -5.0}),
        ("G-03", {"htf_bias": "BEARISH"}),
        ("G-04", {"tp1": 5640.0}),
        ("G-05", {"invalidation_level": 5641.0}),
        ("G-06", {"price_at_analysis": 5300.0}),
        ("G-09", {"tp1": 5654.0, "tp2": 5660.0}),
        ("G-10", {"tp1": 5654.0, "tp2": 5655.0}),
        ("G-11", {"stop_loss": 5652.0, "invalidation_level": 5652.5}),
        ("G-12", {"checklist_positive": 3}),
        ("G-12", {"checklist_negative": 5}),
        ("G-13", {"pda_formed_at_ny": "2026-09-16 06:00"}),
        ("G-15", {"opposing_liquidity_level": 5700.0}),
        ("G-16", {"entry_model": "MARKET_ANCHOR"}),
        ("G-18", {"valid_until_ny": "2026-09-16 07:47"}),
    ],
)
def test_each_rule_has_a_failing_case(ctx, code, overrides):
    assert code in codes(make(**overrides), ctx)


def test_g02_rejects_a_stale_quote(ctx):
    ctx.quote_ts = ctx.now_ts - 600
    assert "G-02" in codes(make(), ctx)


def test_g07_rejects_far_away_levels(ctx):
    far = make(tp3=9000.0)
    assert "G-07" in codes(far, ctx)


def test_g08_rejects_a_setup_already_below_its_array(ctx):
    ctx.bid = 5642.0  # under the stop loss
    assert "G-08" in codes(make(), ctx)


def test_g14_rejects_a_pda_that_already_failed():
    """A respect-timeframe body close beyond the array after formation kills the setup at intake."""
    scenario = clean_long_scenario()
    later = scenario.context_provider()("XAUUSD", ts("2026-09-16 08:10") + 2)
    assert "G-14" not in codes(make(price_at_analysis=5656.4), later)
    broken = Candle(ts("2026-09-16 07:55"), 5655.8, 5656.0, 5639.0, 5640.0)
    later.store.merge("XAUUSD", "M5", [broken], later.now_ts)
    assert "G-14" in codes(make(price_at_analysis=5656.4), later)


def test_g19_duplicate_is_handled_by_the_engine_not_the_gate(ctx):
    assert evaluate_intake(make(), ctx).passed


def test_hard_level_follows_the_profile(ctx):
    setup = make()
    level, name = hard_level(setup, ctx)
    assert level == pytest.approx(setup.pda_ce) and name == "CE"
    ctx.profile = BALANCED
    level, name = hard_level(setup, ctx)
    assert level == pytest.approx(setup.pda_low)
    ctx.profile = STRICT


def test_hard_level_overrides_per_pda_type(ctx):
    assert hard_level(make(pda_type="INVERSION_FVG"), ctx)[0] == pytest.approx(5652.25)
    assert hard_level(make(pda_type="ORDER_BLOCK", pda_mean_threshold=5652.0), ctx)[0] == pytest.approx(5652.0)
    assert hard_level(make(pda_type="BREAKER_BLOCK"), ctx)[0] == pytest.approx(5651.0)
    assert hard_level(make(entry_model="SILVER_BULLET"), ctx)[0] == pytest.approx(5651.0)


def test_l01_sl_touch_by_wick_and_by_quote(ctx):
    setup = make()
    wick = Candle(ctx.now_ts, 5650.0, 5651.0, 5642.0, 5650.0)
    assert not rules.l01_sl_touch(setup, ctx, wick).passed
    ctx.bid = 5643.0
    assert not rules.l01_sl_touch(setup, ctx, None).passed


def test_l02_and_l03_use_body_closes(ctx):
    setup = make()
    wick_through = Candle(ctx.now_ts, 5655.0, 5656.0, 5640.0, 5655.0)
    assert rules.l02_invalidation_close(setup, ctx, wick_through).passed
    assert rules.l03_body_respect(setup, ctx, wick_through).passed
    body_through = Candle(ctx.now_ts, 5655.0, 5656.0, 5640.0, 5645.0)
    assert not rules.l02_invalidation_close(setup, ctx, body_through).passed
    assert not rules.l03_body_respect(setup, ctx, body_through).passed


def test_chase_limit_takes_the_tighter_of_both_caps():
    setup = make()
    strict = rules.chase_limit(setup, STRICT.rr_min_trigger, STRICT.chase_max_risk_frac)
    balanced = rules.chase_limit(setup, BALANCED.rr_min_trigger, BALANCED.chase_max_risk_frac)
    assert strict < balanced  # BALANCED allows more chase and a lower RR floor
    assert strict == pytest.approx(5657.175)


def test_find_cisd_uses_the_bearish_run_then_the_mss_fallback():
    candles = [
        Candle(0, 10, 11, 9, 9.5),
        Candle(60, 9.5, 10, 8, 8.5),
        Candle(120, 8.5, 9, 7, 7.5),
    ]
    assert rules.find_cisd(candles, 2, 3, True, 10, 20) == pytest.approx(10.0)
    bullish = [Candle(i * 60, 10, 12 - abs(i - 2), 9, 11) for i in range(5)]
    assert rules.find_cisd(bullish, 4, 5, True, 10, 20) == pytest.approx(12.0)


def test_t06_spread_limit_is_the_tighter_of_absolute_and_relative(ctx):
    ctx.spread_samples = [0.2] * 60
    ctx.ask = ctx.bid + 0.7
    assert not rules.t06_spread(ctx).passed
    ctx.ask = ctx.bid + 0.3
    assert rules.t06_spread(ctx).passed


def test_verify_pda_reports_unverified_for_non_machine_types(ctx):
    setup = make(pda_type="BREAKER_BLOCK", entry_model="OTE")
    status, _detail = rules.verify_pda(setup, ctx, ts("2026-09-16 07:35"))
    assert status == "UNVERIFIED"
    result = evaluate_intake(setup, ctx)
    assert result.passed and "PDA e paverifikuar" in result.warnings


def test_strict_pda_verify_can_reject_unverified_arrays(ctx):
    from dataclasses import replace

    ctx.profile = replace(STRICT, strict_pda_verify=True)
    assert "G-13" in codes(make(pda_type="OTE_ZONE"), ctx)
    ctx.profile = STRICT
