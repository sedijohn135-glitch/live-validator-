"""Engine behaviour beyond the golden scenarios: outcomes, cancel, status, stats and pause."""

from __future__ import annotations

import json

import pytest

from app.engine import fingerprint, new_setup_id
from app.market import Candle
from app.setup_model import normalise
from tests.test_golden import BASE_SETUP, build, clean_long_scenario, keys, texts, ts


@pytest.fixture()
def triggered(tmp_path):
    scenario = clean_long_scenario()
    scenario.m5(5656.4, 5657.0, 5655.5, 5656.0, at="2026-09-16 08:10")
    engine, store, clock = build(tmp_path, scenario)
    result = engine.submit(dict(BASE_SETUP))
    clock["now"] = ts("2026-09-16 08:10") + 2
    engine.process_symbol("XAUUSD")
    assert store.get_setup(result["setup_id"])["state"] == "TRIGGERED"
    return engine, store, clock, result["setup_id"], scenario


def test_setup_id_shape():
    generated = new_setup_id("XAUUSD", ts("2026-09-16 08:00"))
    prefix, day, tail = generated.split("-")
    assert prefix == "XAU" and day == "0916" and len(tail) == 4


def test_fingerprint_rounds_to_display_decimals():
    a = normalise(dict(BASE_SETUP))
    b = normalise(dict(BASE_SETUP, entry_low=5651.0004))
    assert fingerprint(a, 2) == fingerprint(b, 2)


def test_same_candle_tp_and_sl_counts_as_sl(triggered):
    engine, store, clock, setup_id, scenario = triggered
    row = store.get_setup(setup_id)
    setup, computed = engine._decode(row)
    ctx = scenario.context_provider()("XAUUSD", ts("2026-09-16 08:20"))
    both = Candle(ts("2026-09-16 08:20"), 5656.0, 5680.0, 5640.0, 5650.0)
    engine._track_outcome(setup_id, setup, computed, ctx, both, ts("2026-09-16 08:20"))
    assert store.get_setup(setup_id)["outcome"] == "SL"
    assert any("SL u godit" in t for t in texts(store))
    assert not any("TP1 u arrit" in t for t in texts(store))


def test_tp_messages_are_emitted_once_per_target(triggered):
    engine, store, clock, setup_id, scenario = triggered
    row = store.get_setup(setup_id)
    setup, computed = engine._decode(row)
    ctx = scenario.context_provider()("XAUUSD", ts("2026-09-16 08:20"))
    hit_tp1 = Candle(ts("2026-09-16 08:20"), 5656.0, 5676.0, 5656.0, 5675.0)
    engine._track_outcome(setup_id, setup, computed, ctx, hit_tp1, ts("2026-09-16 08:20"))
    engine._track_outcome(setup_id, setup, computed, ctx, hit_tp1, ts("2026-09-16 08:21"))
    assert len([k for k in keys(store) if k.endswith(":TP1")]) == 1


def test_cancel_rules(tmp_path):
    engine, store, _clock = build(tmp_path, clean_long_scenario())
    result = engine.submit(dict(BASE_SETUP))
    setup_id = result["setup_id"]
    assert engine.cancel("nope")["status"] == "NOT_FOUND"
    assert engine.cancel(setup_id)["status"] == "CANCELLED"
    assert store.get_setup(setup_id)["state"] == "CANCELLED"
    assert engine.cancel(setup_id)["status"] == "ALREADY_CLOSED"
    assert any("u anulua me kërkesë" in t for t in texts(store))


def test_triggered_setup_cannot_be_cancelled(triggered):
    engine, _store, _clock, setup_id, _scenario = triggered
    assert engine.cancel(setup_id)["status"] == "CANNOT_CANCEL"


def test_pause_turns_a_trigger_into_missed(tmp_path):
    engine, store, clock = build(tmp_path, clean_long_scenario())
    result = engine.submit(dict(BASE_SETUP))
    engine.set_paused(True)
    clock["now"] = ts("2026-09-16 08:10") + 2
    engine.process_symbol("XAUUSD")
    assert store.get_setup(result["setup_id"])["state"] == "MISSED"
    assert any("pauzë aktive" in t for t in texts(store))


def test_replacing_an_armed_setup_on_the_same_symbol(tmp_path):
    engine, store, _clock = build(tmp_path, clean_long_scenario())
    ids = iter(["XAU-0916-AAAA", "XAU-0916-BBBB"])
    engine.id_factory = lambda symbol, _now: next(ids)
    first = engine.submit(dict(BASE_SETUP))
    second = engine.submit(dict(BASE_SETUP, tp1=5676.0))
    assert second["status"] == "ARMED"
    assert second["replaced_setup_id"] == first["setup_id"]
    assert store.get_setup(first["setup_id"])["state"] == "REPLACED"
    assert any("u zëvendësua" in t for t in texts(store))


def test_status_reports_distances_and_timeline(tmp_path):
    engine, _store, _clock = build(tmp_path, clean_long_scenario())
    result = engine.submit(dict(BASE_SETUP))
    status = engine.status(result["setup_id"])
    assert status["state"] == "ARMED"
    assert status["live_bid"] == pytest.approx(5658.0)
    assert status["distance_to_sl"] == pytest.approx(15.0)
    assert status["timeline"][0]["type"] == "ARMED"
    overview = engine.status()
    assert len(overview["active"]) == 1 and overview["paused"] is False


def test_opposite_direction_trigger_is_blocked_while_one_is_open(triggered):
    engine, store, clock, setup_id, scenario = triggered
    setup, _computed = engine._decode(store.get_setup(setup_id))
    assert engine._conflicting_trigger(normalise(dict(BASE_SETUP, direction="SHORT", htf_bias="BEARISH")))
    assert not engine._conflicting_trigger(setup)


def test_stats_counts_saves_and_missed_wins(tmp_path):
    engine, store, clock = build(tmp_path, clean_long_scenario())
    result = engine.submit(dict(BASE_SETUP))
    store.execute(
        "UPDATE setups SET state = 'INVALIDATED', shadow_json = ? WHERE id = ?",
        (json.dumps({"eligible": True, "result": "LOSS"}), result["setup_id"]),
    )
    stats = engine.stats(7)
    assert stats["inv"] == 1 and stats["saves"] == 1 and stats["blind_l"] == 1
