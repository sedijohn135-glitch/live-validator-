"""Golden scenarios of validation-rules §14."""

from __future__ import annotations

import pytest

from app.config import BALANCED, STRICT
from app.engine import Engine
from app.store import Store
from tests.scenario import Scenario, settings_for, ts

BASE_SETUP = {
    "symbol": "XAUUSD",
    "direction": "LONG",
    "entry_model": "ICT_2022",
    "htf_timeframe": "H1",
    "htf_bias": "BULLISH",
    "ltf": "M5",
    "entry_low": 5651.0,
    "entry_high": 5653.5,
    "stop_loss": 5643.0,
    "tp1": 5675.0,
    "tp2": 5700.0,
    "invalidation_level": 5647.0,
    "pda_type": "FVG",
    "pda_timeframe": "M5",
    "pda_low": 5651.0,
    "pda_high": 5653.5,
    "pda_formed_at_ny": "2026-09-16 07:35",
    "opposing_liquidity_level": 5646.0,
    "opposing_liquidity_taken": True,
    "price_at_analysis": 5658.0,
    "checklist_positive": 8,
    "checklist_negative": 1,
    "confidence": 85,
    "rationale": "SSL sweep, displacement, FVG retrace.",
    "range_low": 5646.0,
    "range_high": 5680.0,
}


def clean_long_scenario(profile=STRICT, day: str = "2026-09-16") -> Scenario:
    """SSL sweep → displacement FVG → shallow retrace → CISD with displacement at 08:05 NY."""
    s = Scenario(profile=profile).start(f"{day} 05:00")
    s.sweep(f"{day} 07:05", 5646.0)
    s.m5(5650.0, 5651.0, 5649.0, 5650.5, at=f"{day} 07:30")
    s.m5(5650.5, 5657.0, 5650.0, 5656.5)  # 07:35 displacement (FVG middle candle)
    s.m5(5656.5, 5659.0, 5653.5, 5658.0)  # 07:40 → bullish FVG [5651, 5653.5]
    s.m5(5658.0, 5658.5, 5655.0, 5655.3)  # 07:45 bearish
    s.m5(5655.3, 5656.0, 5655.0, 5655.8)  # 07:50 bullish (breaks the bearish run)
    s.m5(5655.8, 5656.0, 5654.0, 5654.2)  # 07:55 bearish
    s.m5(5654.2, 5654.4, 5653.0, 5653.2)  # 08:00 bearish → tap, extreme 5653.0
    s.m5(5653.2, 5656.6, 5653.1, 5656.4)  # 08:05 bullish CISD + displacement
    s.quote(f"{day} 07:46", 5658.0, 5658.2)
    s.quote(f"{day} 08:10", 5656.4, 5656.6)
    return s


def build(tmp_path, scenario: Scenario, profile_name: str = "STRICT", now: float | None = None):
    store = Store(str(tmp_path / "v.db"))
    settings = settings_for(profile_name)
    clock = {"now": now or ts("2026-09-16 07:46")}
    engine = Engine(
        settings,
        store,
        scenario.context_provider(),
        clock=lambda: clock["now"],
        id_factory=lambda symbol, _now: f"{symbol[:3]}-0916-TEST",
    )
    return engine, store, clock


def texts(store) -> list[str]:
    return [row["text"] for row in store.query("SELECT text FROM outbox ORDER BY id")]


def keys(store) -> list[str]:
    return [row["dedupe_key"] for row in store.query("SELECT dedupe_key FROM outbox ORDER BY id")]


# ------------------------------------------------------------------ scenario 1
@pytest.mark.parametrize("profile_name", ["STRICT", "BALANCED"])
def test_01_clean_ict_2022_long_enters_once(tmp_path, profile_name):
    profile = STRICT if profile_name == "STRICT" else BALANCED
    scenario = clean_long_scenario(profile)
    engine, store, clock = build(tmp_path, scenario, profile_name)

    result = engine.submit(dict(BASE_SETUP))
    assert result["status"] == "ARMED", result["reasons"]

    clock["now"] = ts("2026-09-16 08:10") + 2
    engine.process_symbol("XAUUSD")

    row = store.get_setup(result["setup_id"])
    assert row["state"] == "TRIGGERED"
    enters = [k for k in keys(store) if k.endswith(":ENTER")]
    assert len(enters) == 1
    message = next(t for t in texts(store) if "HYR TANI" in t)
    assert "BUY XAUUSD" in message and "Mos hyr nëse çmimi &gt;" in message

    # Re-processing the same data must never produce a second ENTER.
    engine.process_symbol("XAUUSD")
    assert len([k for k in keys(store) if k.endswith(":ENTER")]) == 1


# ------------------------------------------------------------------ scenario 2
@pytest.mark.parametrize("profile_name", ["STRICT", "BALANCED"])
def test_02_fake_out_invalidates_and_counts_as_a_save(tmp_path, profile_name):
    profile = STRICT if profile_name == "STRICT" else BALANCED
    s = Scenario(profile=profile).start("2026-09-16 05:00")
    s.sweep("2026-09-16 07:05", 5646.0)
    s.m5(5650.0, 5651.0, 5649.0, 5650.5, at="2026-09-16 07:30")
    s.m5(5650.5, 5657.0, 5650.0, 5656.5)
    s.m5(5656.5, 5659.0, 5653.5, 5658.0)  # FVG [5651, 5653.5], CE 5652.25
    s.m5(5658.0, 5658.5, 5655.0, 5655.3)
    s.m5(5655.3, 5656.0, 5655.0, 5655.8)
    s.m5(5655.8, 5656.0, 5654.0, 5654.2)
    s.m5(5654.2, 5654.4, 5653.0, 5653.2)  # 08:00 tap
    s.m5(5653.2, 5653.4, 5650.0, 5650.3)  # 08:05 body closes below the array ⇒ L-03
    s.m5(5650.3, 5650.4, 5642.0, 5642.5)  # 08:10 runs through the stop loss
    s.quote("2026-09-16 07:46", 5658.0, 5658.2)
    s.quote("2026-09-16 08:10", 5650.3, 5650.5)
    s.quote("2026-09-16 08:15", 5642.5, 5642.7)
    engine, store, clock = build(tmp_path, s, profile_name)

    result = engine.submit(dict(BASE_SETUP))
    assert result["status"] == "ARMED", result["reasons"]

    clock["now"] = ts("2026-09-16 08:10") + 2
    engine.process_symbol("XAUUSD")
    row = store.get_setup(result["setup_id"])
    assert row["state"] == "INVALIDATED"
    assert not [k for k in keys(store) if k.endswith(":ENTER")]
    assert any("MOS HYR" in t for t in texts(store))

    # The blind-limit trade the owner used to take would have filled and then hit the stop: a save.
    clock["now"] = ts("2026-09-17 09:00")
    ctx = s.context_provider()("XAUUSD", clock["now"])
    assert engine.compute_shadow(result["setup_id"], ctx) == "LOSS"
    stats = engine.stats(7)
    assert stats["saves"] == 1 and stats["ent"] == 0


# ------------------------------------------------------------------ scenario 3
def test_03_runaway_price_is_missed_not_chased(tmp_path):
    s = clean_long_scenario()
    s.bars[-1] = type(s.bars[-1])(s.bars[-1].t, 5653.2, 5670.0, 5653.1, 5669.0)  # 08:05 runs away
    s.quote("2026-09-16 08:10", 5669.0, 5669.2)
    engine, store, clock = build(tmp_path, s)

    assert engine.submit(dict(BASE_SETUP))["status"] == "ARMED"
    clock["now"] = ts("2026-09-16 08:10") + 2
    engine.process_symbol("XAUUSD")

    row = store.query_one("SELECT * FROM setups")
    assert row["state"] == "MISSED"
    assert not [k for k in keys(store) if k.endswith(":ENTER")]
    assert any("Mos e ndiq çmimin" in t for t in texts(store))


# ------------------------------------------------------------------ scenario 4
def test_04a_lunch_blocks_the_trigger(tmp_path):
    s = Scenario().start("2026-09-16 09:00")
    s.sweep("2026-09-16 10:05", 5646.0)
    s.m5(5650.0, 5651.0, 5649.0, 5650.5, at="2026-09-16 11:30")
    s.m5(5650.5, 5657.0, 5650.0, 5656.5)
    s.m5(5656.5, 5659.0, 5653.5, 5658.0)
    s.m5(5658.0, 5658.5, 5655.0, 5655.3)
    s.m5(5655.3, 5656.0, 5655.0, 5655.8)
    s.m5(5655.8, 5656.0, 5654.0, 5654.2)
    s.m5(5654.2, 5654.4, 5653.0, 5653.2)  # 12:00 tap
    s.m5(5653.2, 5656.6, 5653.1, 5656.4)  # 12:05 CISD close at 12:10 NY (lunch)
    s.quote("2026-09-16 11:46", 5658.0, 5658.2)
    s.quote("2026-09-16 12:10", 5656.4, 5656.6)
    engine, store, clock = build(tmp_path, s, now=ts("2026-09-16 11:46"))

    payload = dict(BASE_SETUP, entry_model="LOW_RESISTANCE_RUN", pda_formed_at_ny="2026-09-16 11:35")
    result = engine.submit(payload)
    assert result["status"] == "ARMED", result["reasons"]

    clock["now"] = ts("2026-09-16 12:10") + 2
    engine.process_symbol("XAUUSD")
    assert store.get_setup(result["setup_id"])["state"] == "IN_ZONE"
    assert not [k for k in keys(store) if k.endswith(":ENTER")]


def test_04b_setup_expires_at_the_end_of_its_window(tmp_path):
    s = clean_long_scenario()
    s.m5(5656.4, 5657.0, 5655.5, 5656.0, at="2026-09-16 08:10")
    for index, bar in enumerate(_window_filler(), start=0):
        s.m5(*bar, at=f"2026-09-16 {8 + (15 + index * 5) // 60:02d}:{(15 + index * 5) % 60:02d}")
    s.quote("2026-09-16 08:46", 5656.0, 5656.2)
    s.quote("2026-09-16 09:10", 5656.0, 5656.2)
    engine, store, clock = build(tmp_path, s, now=ts("2026-09-16 08:46"))

    result = engine.submit(dict(BASE_SETUP, price_at_analysis=5656.0, pda_formed_at_ny="2026-09-16 07:35"))
    assert result["status"] == "ARMED", result["reasons"]
    assert result["computed"]["expires_at_ny"] == "2026-09-16 09:00"

    clock["now"] = ts("2026-09-16 09:10") + 2
    engine.process_symbol("XAUUSD")
    row = store.get_setup(result["setup_id"])
    assert row["state"] == "EXPIRED"
    assert not [k for k in keys(store) if k.endswith(":ENTER")]


def _window_filler():
    """Quiet bars from 08:15 to 09:10 that keep the setup alive without triggering."""
    return [(5656.0, 5656.5, 5655.5, 5656.0) for _ in range(12)]


# ------------------------------------------------------------------ scenario 5
def test_05a_outage_over_the_trigger_bar_becomes_missed(tmp_path):
    s = clean_long_scenario()
    engine, store, clock = build(tmp_path, s)
    result = engine.submit(dict(BASE_SETUP))
    assert result["status"] == "ARMED"

    clock["now"] = ts("2026-09-16 08:10") + 300  # feed restored five minutes late
    engine.process_symbol("XAUUSD")
    row = store.get_setup(result["setup_id"])
    assert row["state"] == "MISSED"
    assert not [k for k in keys(store) if k.endswith(":ENTER")]
    assert any("gjatë ndërprerjes" in t for t in texts(store))


def test_05b_short_outage_still_enters_with_a_delay_note(tmp_path):
    s = clean_long_scenario()
    engine, store, clock = build(tmp_path, s)
    result = engine.submit(dict(BASE_SETUP))

    clock["now"] = ts("2026-09-16 08:10") + 40
    engine.process_symbol("XAUUSD")
    row = store.get_setup(result["setup_id"])
    assert row["state"] == "TRIGGERED"
    message = next(t for t in texts(store) if "HYR TANI" in t)
    assert "vonesë 40s" in message


# ------------------------------------------------------------------ scenario 6
def test_06_restart_between_transaction_and_send_delivers_exactly_once(tmp_path):
    import asyncio

    from app.telegram import OutboxSender, TelegramClient

    s = clean_long_scenario()
    engine, store, clock = build(tmp_path, s)
    result = engine.submit(dict(BASE_SETUP))
    clock["now"] = ts("2026-09-16 08:10") + 2
    engine.process_symbol("XAUUSD")
    assert store.get_setup(result["setup_id"])["state"] == "TRIGGERED"
    store.close()

    # Process crashes before the sender runs; a fresh process opens the same database.
    store2 = Store(str(tmp_path / "v.db"))
    engine2 = Engine(
        settings_for("STRICT"),
        store2,
        s.context_provider(),
        clock=lambda: ts("2026-09-16 08:10") + 4,
        id_factory=lambda symbol, _now: f"{symbol[:3]}-0916-TEST2",
    )
    engine2.process_symbol("XAUUSD")
    assert len([k for k in keys(store2) if k.endswith(":ENTER")]) == 1

    calls: list[dict] = []

    async def fake(method, payload):
        calls.append({"method": method, **payload})
        return {"message_id": len(calls)}

    sender = OutboxSender(store2, TelegramClient("token", fake), "42")
    asyncio.run(sender.drain_once())
    asyncio.run(sender.drain_once())
    enters = [c for c in calls if "HYR TANI" in c.get("text", "")]
    assert len(enters) == 1


# ------------------------------------------------------------------ scenario 7
SHORT_SETUP = dict(
    BASE_SETUP,
    direction="SHORT",
    htf_bias="BEARISH",
    entry_low=5658.0,
    entry_high=5660.0,
    stop_loss=5670.0,
    tp1=5630.0,
    tp2=5600.0,
    invalidation_level=5665.0,
    pda_low=5658.0,
    pda_high=5660.0,
    opposing_liquidity_level=5662.0,
    opposing_liquidity_taken=False,
    price_at_analysis=5658.0,
    range_low=5600.0,
    range_high=5700.0,
)


def codes(result) -> list[str]:
    return [r["code"] for r in result["reasons"]]


def test_07_short_near_the_lookback_high_is_rejected(tmp_path):
    engine, _store, _clock = build(tmp_path, clean_long_scenario())
    result = engine.submit(dict(SHORT_SETUP))
    assert result["status"] == "REJECTED"
    assert "G-17" in codes(result)


def test_07b_sm_three_stage_is_exempt_from_the_ath_filter(tmp_path):
    engine, _store, _clock = build(tmp_path, clean_long_scenario())
    result = engine.submit(dict(SHORT_SETUP, entry_model="SM_THREE_STAGE"))
    assert "G-17" not in codes(result)


# ------------------------------------------------------------------ scenario 8
def test_08_hallucinated_prices_are_rejected(tmp_path):
    engine, _store, _clock = build(tmp_path, clean_long_scenario())
    hallucinated = dict(
        BASE_SETUP,
        price_at_analysis=2650.0,
        entry_low=2648.0,
        entry_high=2652.0,
        stop_loss=2640.0,
        tp1=2680.0,
        tp2=2700.0,
        invalidation_level=2644.0,
        pda_low=2648.0,
        pda_high=2652.0,
        opposing_liquidity_level=2642.0,
    )
    result = engine.submit(hallucinated)
    assert result["status"] == "REJECTED"
    assert "G-06" in codes(result) and "G-07" in codes(result)


# ------------------------------------------------------------------ scenario 9
def test_09_declared_fvg_missing_from_the_data_is_rejected(tmp_path):
    engine, _store, _clock = build(tmp_path, clean_long_scenario())
    absent = dict(
        BASE_SETUP,
        entry_low=5620.0,
        entry_high=5622.0,
        stop_loss=5612.0,
        invalidation_level=5616.0,
        tp1=5700.0,
        tp2=5760.0,
        pda_low=5620.0,
        pda_high=5622.0,
        opposing_liquidity_level=5615.0,
        opposing_liquidity_taken=False,
    )
    result = engine.submit(absent)
    assert result["status"] == "REJECTED"
    assert "G-13" in codes(result)


# ----------------------------------------------------------------- scenario 10
def test_10_false_liquidity_claim_is_rejected(tmp_path):
    s = Scenario().start("2026-09-16 05:00")
    s.sweep("2026-09-14 06:00", 5646.0)  # real, but far outside the liquidity window
    s.m5(5650.0, 5651.0, 5649.0, 5650.5, at="2026-09-16 07:30")
    s.m5(5650.5, 5657.0, 5650.0, 5656.5)
    s.m5(5656.5, 5659.0, 5653.5, 5658.0)
    s.quote("2026-09-16 07:46", 5658.0, 5658.2)
    engine, _store, _clock = build(tmp_path, s)
    result = engine.submit(dict(BASE_SETUP))
    assert result["status"] == "REJECTED"
    assert "G-15" in codes(result)
    assert result["computed"]["liquidity_check"]["reason"] == "false_claim"


# ----------------------------------------------------------------- scenario 15
def test_15_duplicate_submit_returns_the_same_setup(tmp_path):
    engine, store, _clock = build(tmp_path, clean_long_scenario())
    first = engine.submit(dict(BASE_SETUP))
    second = engine.submit(dict(BASE_SETUP))
    assert first["status"] == "ARMED"
    assert second["status"] == "DUPLICATE"
    assert second["setup_id"] == first["setup_id"]
    assert len(store.query("SELECT id FROM setups")) == 1
    assert len([k for k in keys(store) if k.endswith(":ARMED")]) == 1


# ----------------------------------------------------------------- scenario 16
@pytest.mark.parametrize("day", ["2026-03-09", "2026-11-02", "2026-09-16"])
def test_16_dst_days_keep_the_kill_zone_and_expiry(tmp_path, day):
    s = clean_long_scenario(day=day)
    engine, store, clock = build(tmp_path, s, now=ts(f"{day} 07:46"))
    result = engine.submit(dict(BASE_SETUP, pda_formed_at_ny=f"{day} 07:35"))
    assert result["status"] == "ARMED", result["reasons"]
    assert result["computed"]["expires_at_ny"] == f"{day} 09:00"

    clock["now"] = ts(f"{day} 08:10") + 2
    engine.process_symbol("XAUUSD")
    assert store.get_setup(result["setup_id"])["state"] == "TRIGGERED"


# ----------------------------------------------------------------- scenario 11
def turtle_soup_scenario(rejection_low: float) -> Scenario:
    s = Scenario().start("2026-09-16 05:00")
    s.sweep("2026-09-16 07:05", 5646.0)
    s.m5(5650.0, 5651.0, 5649.0, 5650.5, at="2026-09-16 07:30")
    s.m5(5650.5, 5657.0, 5650.0, 5656.5)  # 07:35 displacement
    s.m5(5656.5, 5659.0, 5653.5, 5658.0)  # 07:40 → FVG [5651, 5653.5], CE 5652.25
    s.m5(5658.0, 5658.5, 5654.0, 5654.5)  # 07:45
    s.m5(5654.5, 5656.0, rejection_low, 5655.5)  # 07:50 rejection candle
    s.quote("2026-09-16 07:46", 5654.5, 5654.7)
    s.quote("2026-09-16 07:55", 5655.5, 5655.7)
    return s


TSD_SETUP = dict(
    BASE_SETUP,
    entry_model="TURTLE_SOUP_DEFERRED",
    range_low=5600.0,
    range_high=5720.0,
    price_at_analysis=5654.5,
)


def test_11_turtle_soup_deferred_enters_on_the_rejection_candle(tmp_path):
    engine, store, clock = build(tmp_path, turtle_soup_scenario(5652.5))
    result = engine.submit(dict(TSD_SETUP))
    assert result["status"] == "ARMED", result["reasons"]

    clock["now"] = ts("2026-09-16 07:55") + 2
    engine.process_symbol("XAUUSD")
    assert store.get_setup(result["setup_id"])["state"] == "TRIGGERED"
    assert len([k for k in keys(store) if k.endswith(":ENTER")]) == 1


def test_11b_wick_below_ce_does_not_trigger(tmp_path):
    engine, store, clock = build(tmp_path, turtle_soup_scenario(5650.0))
    result = engine.submit(dict(TSD_SETUP))
    assert result["status"] == "ARMED", result["reasons"]

    clock["now"] = ts("2026-09-16 07:55") + 2
    engine.process_symbol("XAUUSD")
    assert store.get_setup(result["setup_id"])["state"] == "IN_ZONE"
    assert not [k for k in keys(store) if k.endswith(":ENTER")]


# ----------------------------------------------------------------- scenario 12
def venom_scenario() -> Scenario:
    s = Scenario().start("2026-09-16 05:00")
    s.m5(5650.0, 5651.0, 5649.0, 5650.0, at="2026-09-16 07:20")
    s.m5(5650.0, 5650.2, 5644.0, 5644.5)  # 07:25 displacement down
    s.m5(5644.5, 5648.5, 5643.0, 5644.0)  # 07:30 → SIBI [5648.5, 5649]
    s.m5(5644.0, 5645.0, 5643.5, 5644.5)  # 07:35
    s.m5(5644.5, 5649.2, 5644.4, 5648.8)  # 07:40 BISI middle candle, close = model_ref_level
    s.m5(5648.8, 5650.0, 5648.0, 5649.5)  # 07:45 → BISI [5645, 5648]
    s.m5(5649.5, 5650.0, 5648.5, 5648.6)  # 07:50 price returns to the BISI close
    s.quote("2026-09-16 07:51", 5648.6, 5648.8)
    s.quote("2026-09-16 07:55", 5648.5, 5648.7)
    return s


VENOM_SETUP = dict(
    BASE_SETUP,
    entry_model="VENOM",
    entry_low=5648.0,
    entry_high=5649.0,
    stop_loss=5638.0,
    tp1=5665.0,
    tp2=5680.0,
    invalidation_level=5642.0,
    pda_low=5645.0,
    pda_high=5648.0,
    pda_formed_at_ny="2026-09-16 07:40",
    model_ref_level=5648.8,
    opposing_liquidity_level=5643.0,
    opposing_liquidity_taken=True,
    price_at_analysis=5648.6,
    range_low=5640.0,
    range_high=5660.0,
)


def test_12_venom_triggers_on_the_bisi_close(tmp_path):
    engine, store, clock = build(tmp_path, venom_scenario(), now=ts("2026-09-16 07:51"))
    result = engine.submit(dict(VENOM_SETUP))
    assert result["status"] == "ARMED", result["reasons"]

    clock["now"] = ts("2026-09-16 07:55") + 2
    engine.process_symbol("XAUUSD")
    assert store.get_setup(result["setup_id"])["state"] == "TRIGGERED"


def test_12b_short_venom_is_rejected(tmp_path):
    engine, _store, _clock = build(tmp_path, venom_scenario(), now=ts("2026-09-16 07:51"))
    result = engine.submit(dict(VENOM_SETUP, direction="SHORT", htf_bias="BEARISH"))
    assert result["status"] == "REJECTED"
    assert "G-16" in codes(result)


# ----------------------------------------------------------------- scenario 13
def test_13a_silver_bullet_outside_its_window_is_rejected(tmp_path):
    engine, _store, _clock = build(tmp_path, clean_long_scenario())
    result = engine.submit(dict(BASE_SETUP, entry_model="SILVER_BULLET", pda_formed_at_ny="2026-09-16 04:05"))
    assert result["status"] == "REJECTED"
    assert "G-16" in codes(result)
    assert "Silver Bullet" in next(r["text"] for r in result["reasons"] if r["code"] == "G-16")


def test_13b_silver_bullet_with_a_close_target_is_rejected(tmp_path):
    s = clean_long_scenario()
    engine, _store, _clock = build(tmp_path, s, now=ts("2026-09-16 03:40"))
    close_target = dict(
        BASE_SETUP,
        entry_model="SILVER_BULLET",
        pda_formed_at_ny="2026-09-16 03:20",
        tp1=5656.0,
        tp2=None,
        price_at_analysis=5650.0,
    )
    result = engine.submit(close_target)
    assert result["status"] == "REJECTED"
    assert "G-16" in codes(result)
    assert "DOL" in next(r["text"] for r in result["reasons"] if r["code"] == "G-16")


# ----------------------------------------------------------------- scenario 14
@pytest.mark.parametrize("profile_name", ["STRICT", "BALANCED"])
def test_14_spread_spike_over_two_bars_is_missed(tmp_path, profile_name):
    profile = STRICT if profile_name == "STRICT" else BALANCED
    s = clean_long_scenario(profile)
    s.m5(5656.4, 5659.0, 5656.3, 5658.8, at="2026-09-16 08:10")
    s.spread_sample = 0.2
    s.quote("2026-09-16 08:10", 5656.4, 5657.1)  # spread 0.7 vs median 0.2
    s.quote("2026-09-16 08:15", 5658.8, 5659.5)
    engine, store, clock = build(tmp_path, s, profile_name)
    result = engine.submit(dict(BASE_SETUP))
    assert result["status"] == "ARMED", result["reasons"]

    clock["now"] = ts("2026-09-16 08:10") + 2
    engine.process_symbol("XAUUSD")
    assert store.get_setup(result["setup_id"])["state"] == "IN_ZONE"

    clock["now"] = ts("2026-09-16 08:15") + 2
    engine.process_symbol("XAUUSD")
    row = store.get_setup(result["setup_id"])
    assert row["state"] == "MISSED"
    assert any("spread i lartë" in t for t in texts(store))
    assert not [k for k in keys(store) if k.endswith(":ENTER")]


# ----------------------------------------------------------------- scenario 17
def test_17_exit_messages_are_sent_at_most_once_per_reason(tmp_path):
    import json

    s = clean_long_scenario()
    s.m5(5656.4, 5657.0, 5640.0, 5641.0, at="2026-09-16 08:10")  # closes beyond the invalidation level
    s.quote("2026-09-16 08:15", 5641.0, 5641.2)
    engine, store, clock = build(tmp_path, s)
    result = engine.submit(dict(BASE_SETUP))
    clock["now"] = ts("2026-09-16 08:10") + 2
    engine.process_symbol("XAUUSD")
    setup_id = result["setup_id"]
    assert store.get_setup(setup_id)["state"] == "TRIGGERED"

    # Three same-direction arrays below the entry, all broken by the 08:10 collapse.
    row = store.get_setup(setup_id)
    computed = json.loads(row["computed_json"])
    computed["entry_fvgs"] = [
        {"low": 5650.0, "high": 5651.0, "formed_at": ts("2026-09-16 07:35")},
        {"low": 5648.0, "high": 5649.0, "formed_at": ts("2026-09-16 07:35")},
        {"low": 5646.0, "high": 5647.0, "formed_at": ts("2026-09-16 07:35")},
    ]
    store.execute(
        "UPDATE setups SET computed_json = ? WHERE id = ?",
        (json.dumps(computed), setup_id),
    )

    clock["now"] = ts("2026-09-16 08:15") + 2
    engine.process_symbol("XAUUSD")
    engine.process_symbol("XAUUSD")
    exits = [k for k in keys(store) if ":EXIT_" in k]
    assert sorted(exits) == [f"{setup_id}:EXIT_ARRAYS", f"{setup_id}:EXIT_INVALIDATION"]
    assert len([t for t in texts(store) if "DIL NGA TREGU" in t]) == 2
