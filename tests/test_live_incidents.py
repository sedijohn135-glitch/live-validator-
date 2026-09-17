"""The three rejections seen in live testing on XAUUSD, each reproduced then fixed.

G-02 on a tick-feed hiccup, G-18 on a Last Hour setup, G-15 on liquidity swept earlier in the day.
"""

from __future__ import annotations

import pytest

from app import rules
from app.ctrader import Quote
from app.engine import Engine
from app.rules import compute_expiry, evaluate_intake, g02_data, g15_liquidity, t07_price
from app.setup_model import normalise
from app.store import Store
from app.timeutil import in_model_windows, model_windows, ny_string, parse_ny
from tests.app_harness import make_runtime
from tests.test_golden import BASE_SETUP, SHORT_SETUP, clean_long_scenario, settings_for, texts, ts


def codes(result) -> list[str]:
    return [f.code for f in result.failures]


# --------------------------------------------------------------------- G-02
def load_candles(runtime, scenario, now: float) -> None:
    context = scenario.context_provider()("XAUUSD", now)
    for timeframe in ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"):
        series = context.store.series("XAUUSD", timeframe)
        if series:
            runtime.candles.merge("XAUUSD", timeframe, series, now + 10**6)


def test_a_dead_tick_feed_no_longer_blocks_arming(tmp_path):
    """The incident: setup_submit rejected with G-02 while the candle history was perfectly fresh."""
    runtime, _fake, _tg = make_runtime(tmp_path)
    now = ts("2026-09-16 08:10") + 2
    runtime.clock = lambda: now
    load_candles(runtime, clean_long_scenario(), now)
    runtime.quotes.clear()  # the tick stream dropped

    quote = runtime.quote_for("XAUUSD", now)
    assert quote is not None and quote.synthetic
    assert quote.bid == pytest.approx(5656.4)  # the newest M1 close

    ctx = runtime.make_context("XAUUSD", now)
    assert ctx.quote_synthetic is True
    assert g02_data(normalise(dict(BASE_SETUP)), ctx).passed


def test_a_stand_in_price_can_arm_but_never_enter(tmp_path):
    """The safety half of the fix: a candle close is not a price to enter at."""
    runtime, _fake, _tg = make_runtime(tmp_path)
    now = ts("2026-09-16 08:10") + 2
    runtime.clock = lambda: now
    load_candles(runtime, clean_long_scenario(), now)
    runtime.quotes.clear()

    setup = normalise(dict(BASE_SETUP))
    ctx = runtime.make_context("XAUUSD", now)
    assert evaluate_intake(setup, ctx).passed  # arming is allowed

    decision = t07_price(setup, ctx)
    assert not decision.passed
    assert decision.data.get("synthetic") is True  # the trigger refuses it


def test_a_live_tick_always_wins_over_the_stand_in(tmp_path):
    runtime, _fake, _tg = make_runtime(tmp_path)
    now = ts("2026-09-16 08:10") + 2
    runtime.clock = lambda: now
    load_candles(runtime, clean_long_scenario(), now)
    runtime.quotes["XAUUSD"] = Quote("XAUUSD", 5656.4, 5656.6, now)

    quote = runtime.quote_for("XAUUSD", now)
    assert quote is not None and not quote.synthetic
    assert runtime.make_context("XAUUSD", now).quote_synthetic is False


def test_a_truly_ancient_price_is_still_refused(tmp_path):
    """The fix must not become blindness: nothing usable means nothing arms."""
    runtime, _fake, _tg = make_runtime(tmp_path)
    now = ts("2026-09-16 08:10") + 2
    runtime.clock = lambda: now
    ctx = runtime.make_context("XAUUSD", now)  # no candles at all, no quote
    assert ctx.bid is None
    assert not g02_data(normalise(dict(BASE_SETUP)), ctx).passed


# --------------------------------------------------------------------- G-18
def test_last_hour_is_a_tradeable_window_for_the_pm_continuation():
    """The incident: a 14:55 Last Hour setup was rejected for having no kill zone left."""
    for profile_is_balanced in (False, True):
        windows = model_windows("LUNCH_MACRO_PM", None, profile_is_balanced)
        assert [(w.start_min, w.end_min) for w in windows] == [(13 * 60 + 30, 16 * 60)]
        assert in_model_windows("LUNCH_MACRO_PM", None, profile_is_balanced, parse_ny("2026-09-17 15:30"))


def test_pm_session_models_already_cover_the_last_hour():
    for model in ("MARKET_ANCHOR", "OTE", "IOFED", "LOW_RESISTANCE_RUN", "VENOM", "SM_THREE_STAGE"):
        assert in_model_windows(model, None, False, parse_ny("2026-09-17 15:30")), model


def test_silver_bullet_still_refuses_the_last_hour():
    """v11 Iron Rule 13: 3-4 PM is NOT a Silver Bullet window. The fix must not break that."""
    formed = parse_ny("2026-09-17 14:10")
    assert in_model_windows("SILVER_BULLET", formed, False, parse_ny("2026-09-17 14:30")) == "PM_SB"
    assert in_model_windows("SILVER_BULLET", formed, False, parse_ny("2026-09-17 15:30")) is None


def test_a_setup_submitted_at_1455_expires_at_the_cash_close(tmp_path):
    scenario = clean_long_scenario()
    now = ts("2026-09-17 14:55")
    ctx = scenario.context_provider()("XAUUSD", now)
    ctx.now_ts = now
    setup = normalise(dict(BASE_SETUP, entry_model="LUNCH_MACRO_PM", pda_formed_at_ny="2026-09-17 13:45"))
    expires_at, windows = compute_expiry(setup, ctx)
    assert windows == ["LUNCH_MACRO_PM"]
    assert ny_string(expires_at) == "2026-09-17 16:00"
    assert expires_at - now >= 3600


def test_lunch_macro_pm_at_the_last_hour_arms(tmp_path):
    """The end-to-end case the report asks for: LUNCH_MACRO_PM, kill_zone LAST_HOUR, status ARMED."""
    scenario = clean_long_scenario(day="2026-09-17")
    for _ in range(80):  # quiet bars carrying the series into the afternoon
        scenario.m5(5656.0, 5656.6, 5655.4, 5656.0)
    scenario.quote("2026-09-17 14:55", 5658.0, 5658.2)
    store = Store(str(tmp_path / "v.db"))
    clock = {"now": ts("2026-09-17 14:55")}
    engine = Engine(
        settings_for("STRICT"),
        store,
        scenario.context_provider(),
        clock=lambda: clock["now"],
        id_factory=lambda symbol, _now: f"{symbol[:3]}-0917-LAST",
    )
    result = engine.submit(
        dict(
            BASE_SETUP,
            entry_model="LUNCH_MACRO_PM",
            pda_formed_at_ny="2026-09-17 07:35",
            kill_zone="LAST_HOUR",
            macro="PM Session",
        )
    )
    assert "G-18" not in [r["code"] for r in result["reasons"]], result["reasons"]
    assert "G-02" not in [r["code"] for r in result["reasons"]], result["reasons"]
    assert result["status"] == "ARMED", result["reasons"]
    assert result["computed"]["expires_at_ny"] == "2026-09-17 16:00"
    assert any("SETUP NË MONITORIM" in t for t in texts(store))


# --------------------------------------------------------------------- G-15
def short_setup(ctx, **overrides) -> object:
    return normalise(dict(SHORT_SETUP, **overrides))


def test_liquidity_swept_earlier_in_the_day_is_accepted(tmp_path):
    """The incident: a PM short was rejected because the swept PDH sat above its stop loss."""
    scenario = clean_long_scenario()
    ctx = scenario.context_provider()("XAUUSD", ts("2026-09-16 07:46"))
    overhead = ctx.level("lookback_high")
    assert overhead > SHORT_SETUP["stop_loss"], "the test needs a pool beyond the stop"

    setup = short_setup(ctx, opposing_liquidity_level=overhead, opposing_liquidity_taken=False)
    result = g15_liquidity(setup, ctx)
    assert result.passed, result.data


def test_liquidity_on_the_wrong_side_is_still_rejected(tmp_path):
    """Relaxing placement must not accept a pool below a short's entry."""
    scenario = clean_long_scenario()
    ctx = scenario.context_provider()("XAUUSD", ts("2026-09-16 07:46"))
    below = ctx.level("lookback_low")
    setup = short_setup(ctx, opposing_liquidity_level=below, opposing_liquidity_taken=False)
    result = g15_liquidity(setup, ctx)
    assert not result.passed and result.data["reason"] == "placement"


def test_a_long_still_needs_its_pool_below_the_entry(tmp_path):
    scenario = clean_long_scenario()
    ctx = scenario.context_provider()("XAUUSD", ts("2026-09-16 07:46"))
    above = ctx.level("lookback_high")
    setup = normalise(dict(BASE_SETUP, opposing_liquidity_level=above, opposing_liquidity_taken=False))
    assert not g15_liquidity(setup, ctx).passed


def test_a_false_sweep_claim_is_still_rejected(tmp_path):
    """The check that actually matters stays: claiming a sweep the data does not show."""
    scenario = clean_long_scenario()
    ctx = scenario.context_provider()("XAUUSD", ts("2026-09-16 07:46"))
    overhead = ctx.level("lookback_high")
    setup = short_setup(ctx, opposing_liquidity_level=overhead, opposing_liquidity_taken=True)
    result = g15_liquidity(setup, ctx)
    assert not result.passed and result.data["reason"] == "false_claim"


def test_daily_swings_count_as_real_liquidity(tmp_path):
    scenario = clean_long_scenario()
    ctx = scenario.context_provider()("XAUUSD", ts("2026-09-16 07:46"))
    daily = ctx.candles("D1")
    swing = max(c.h for c in daily[-10:])
    setup = short_setup(ctx, opposing_liquidity_level=swing, opposing_liquidity_taken=False)
    assert rules.liquidity_is_real(setup, ctx)


def test_the_incident_setup_would_pass_placement_today():
    """The exact geometry from the live report: entry 4356.50-4357.94, SL 4363.90, BSL 4367.83."""
    from app.config import STRICT, SYMBOL_DEFAULTS
    from app.context import MarketContext
    from app.market import CandleStore

    ctx = MarketContext(
        symbol="XAUUSD",
        now_ts=ts("2026-09-17 14:55"),
        profile=STRICT,
        sym=SYMBOL_DEFAULTS["XAUUSD"],
        store=CandleStore(close_grace_s=0.0),
        bid=4357.0,
        ask=4357.1,
        quote_ts=ts("2026-09-17 14:55"),
        spread_samples=[0.1] * 60,
    )
    ctx.levels = {"pdh": 4367.83}
    setup = normalise(
        dict(
            SHORT_SETUP,
            entry_low=4356.50,
            entry_high=4357.94,
            stop_loss=4363.90,
            tp1=4343.28,
            tp2=4323.49,
            tp3=4305.16,
            invalidation_level=4361.80,
            pda_low=4356.50,
            pda_high=4357.94,
            opposing_liquidity_level=4367.83,
            opposing_liquidity_taken=False,
            price_at_analysis=4357.0,
        )
    )
    result = g15_liquidity(setup, ctx)
    assert result.passed, result.data
