"""Whole lifecycles: register → touch → evidence → ENTER or LIMIT → secure → close."""

from __future__ import annotations

import pytest

from tests.synth import Feed

ZONE = {"entry_low": 4295.0, "entry_high": 4300.0, "stop_loss": 4288.0, "tp1": 4330.0}


def quiet_approach(feed: Feed, target: float = 4302.0, bars: int = 30) -> None:
    """Walk the tape down to just above the zone so the candles and the quote tell the same story.

    It stops short of 4300 on purpose: the engine now reads the range price covered between passes,
    so a tape that dips into the zone here is a real touch and the first tick would no longer be
    the first touch.
    """
    step = (target - feed.tape.price) / bars
    feed.tape.drift(bars, step=step, span=0.5)


def plan_of(feed: Feed, setup_id: str) -> dict:
    import json

    return json.loads(feed.store.get_setup(setup_id)["computed_json"])["plan"]


def confirm_long(feed: Feed) -> None:
    """A sweep-and-reclaim plus a strong body: the balance the validator asks for."""
    feed.tape.sweep(low=4291.0, close=4298.0)
    atr = feed.tape.context().atr("M1") or 1.0
    feed.tape.push(4298.0, 4298.0 + 2 * atr, 4297.8, 4298.0 + 1.8 * atr)


def test_every_setup_is_registered_whatever_it_looks_like(tmp_path):
    feed = Feed(tmp_path, price=4320.0)
    for payload in (
        {"entry": 4300.0, "stop_loss": 4290.0},
        {"entry_low": 4305.0, "entry_high": 4295.0, "stop_loss": 4288.0},  # inverted zone
        {"direction": "LONG", "entry": 4300.0, "stop_loss": 4310.0},  # contradictory direction
        {"entry": 4300.0, "stop_loss": 4290.0, "tp1": 4280.0},  # target on the wrong side
    ):
        result = feed.submit(**payload)
        assert result["status"] == "registered", payload
    assert len([m for m in feed.messages() if "SETUP I REGJISTRUAR" in m]) == 4


def test_a_payload_without_numbers_is_the_only_refusal(tmp_path):
    feed = Feed(tmp_path)
    assert feed.submit(note="blej arin")["status"] == "unusable"


def test_the_full_path_from_registration_to_enter_now(tmp_path):
    feed = Feed(tmp_path, price=4320.0)
    setup_id = feed.submit(**ZONE)["setup_id"]
    quiet_approach(feed)

    feed.tick(price=4310.0)
    assert feed.state(setup_id) == "WATCHING"

    feed.tick(price=4298.0)  # first touch: never an entry
    assert feed.state(setup_id) == "AT_ZONE"
    assert "ZONA U PREK" in feed.last_message()

    confirm_long(feed)
    feed.tick(price=4301.0)
    assert feed.state(setup_id) == "ENTERED"
    text = feed.last_message()
    assert "HYR TANI" in text
    assert "Siguro fitimet" in text
    assert "Evidenca:" in text


def test_a_zone_crossed_between_two_passes_still_counts_as_touched(tmp_path):
    """The BTC-0919-NFP6 incident, in miniature.

    A SHORT with its zone above the price. Price climbed through the whole band and on to the stop
    while the engine was between passes, so no poll ever landed inside the zone. The owner was told
    the setup was cancelled and never that the zone had been reached at all — the one message that
    says the setup is live. Judged on the range price covered, the touch is not missable.
    """
    feed = Feed(tmp_path, price=4300.0)
    setup_id = feed.submit(entry_low=4320.0, entry_high=4325.0, stop_loss=4340.0, tp1=4280.0)["setup_id"]
    feed.tick(price=4300.0)
    assert feed.state(setup_id) == "WATCHING"

    # One minute in which price walks 4300 → 4330, straight through the zone, seen by no poll.
    feed.tape.push(4300.0, 4330.0, 4300.0, 4330.0)  # o, h, l, c: one minute across the whole band
    feed.tick(price=4330.0)

    assert feed.state(setup_id) == "AT_ZONE", "the band was crossed, so the zone was touched"
    assert "ZONA U PREK" in feed.last_message()


def test_a_stop_crossed_between_two_passes_still_cancels(tmp_path):
    """The same reading in the other direction: a level jumped over is still a level reached."""
    feed = Feed(tmp_path, price=4300.0)
    setup_id = feed.submit(entry_low=4320.0, entry_high=4325.0, stop_loss=4340.0, tp1=4280.0)["setup_id"]
    feed.tick(price=4300.0)

    feed.tape.push(4300.0, 4345.0, 4300.0, 4335.0)
    feed.tick(price=4335.0)

    assert feed.outcome(setup_id) == "CANCELLED_SL_FIRST"
    assert "SETUPI U ANULUA" in feed.last_message()


def test_a_runaway_price_becomes_a_limit_not_a_chase(tmp_path):
    feed = Feed(tmp_path, price=4320.0)
    setup_id = feed.submit(**ZONE)["setup_id"]
    quiet_approach(feed)
    feed.tick(price=4298.0)
    confirm_long(feed)

    feed.tick(price=4312.0)  # more than 0.35R beyond the zone
    assert feed.state(setup_id) == "LIMIT"
    assert "LIMIT" in feed.last_message()

    feed.tape.drift(2, step=-1.0)
    row = feed.store.get_setup(setup_id)
    feed.tick(price=row["entry_price"] - 0.1)
    assert feed.state(setup_id) == "ENTERED"
    assert "LIMIT U MBUSH" in feed.last_message()


def test_the_stop_before_the_entry_cancels_the_setup(tmp_path):
    feed = Feed(tmp_path, price=4320.0)
    setup_id = feed.submit(**ZONE)["setup_id"]
    quiet_approach(feed)
    feed.tick(price=4287.0)
    assert feed.state(setup_id) == "DONE"
    assert feed.outcome(setup_id) == "CANCELLED_SL_FIRST"
    assert "SL u prek para" in feed.last_message()


def test_tp1_before_the_entry_cancels_the_setup(tmp_path):
    feed = Feed(tmp_path, price=4320.0)
    setup_id = feed.submit(**ZONE)["setup_id"]
    quiet_approach(feed)
    feed.tick(price=4331.0)
    assert feed.outcome(setup_id) == "CANCELLED_TP1_FIRST"
    assert "TP1 u prek para" in feed.last_message()


def test_nothing_else_ever_cancels_a_setup(tmp_path):
    """No expiry, no session end, no news: an untouched setup simply waits."""
    feed = Feed(tmp_path, price=4320.0)
    setup_id = feed.submit(**ZONE)["setup_id"]
    for _ in range(40):
        feed.tape.drift(30, step=0.0, span=0.4)
        feed.tick(price=4315.0)
    assert feed.state(setup_id) == "WATCHING"


def test_after_entry_the_owner_is_told_where_to_secure_the_profit(tmp_path):
    feed = Feed(tmp_path, price=4320.0)
    setup_id = feed.submit(**ZONE)["setup_id"]
    quiet_approach(feed)
    feed.tick(price=4298.0)
    confirm_long(feed)
    feed.tick(price=4301.0)
    built = plan_of(feed, setup_id)

    feed.tape.drift(2, step=1.0)
    feed.tick(price=built["secure_at"] + 0.05)
    assert "SIGURO FITIMET" in feed.last_message()

    feed.tape.drift(1, step=1.0)
    feed.tick(price=built["entry"] + built["risk"] + 0.05)
    assert "SL NË HYRJE" in feed.last_message()


def test_a_target_hit_is_reported_and_the_last_one_closes_the_setup(tmp_path):
    feed = Feed(tmp_path, price=4320.0)
    setup_id = feed.submit(entry_low=4295.0, entry_high=4300.0, stop_loss=4288.0, tp1=4310.0)["setup_id"]
    quiet_approach(feed)
    feed.tick(price=4298.0)
    confirm_long(feed)
    feed.tick(price=4301.0)
    assert feed.state(setup_id) == "ENTERED"

    feed.tape.drift(2, step=1.0)
    feed.tick(price=4311.0)
    assert "TP1 U ARRIT" in feed.last_message()
    assert feed.state(setup_id) == "DONE"
    assert feed.outcome(setup_id) == "TP1"


def test_the_stop_after_entry_closes_the_setup(tmp_path):
    feed = Feed(tmp_path, price=4320.0)
    setup_id = feed.submit(**ZONE)["setup_id"]
    quiet_approach(feed)
    feed.tick(price=4298.0)
    confirm_long(feed)
    feed.tick(price=4301.0)

    feed.tape.drift(2, step=-1.0)
    feed.tick(price=plan_of(feed, setup_id)["stop"] - 0.5)
    assert feed.outcome(setup_id) == "SL"
    assert "SL U PREK" in feed.last_message()


def test_evidence_progress_is_reported_but_throttled(tmp_path):
    feed = Feed(tmp_path, price=4320.0)
    feed.submit(**ZONE)
    quiet_approach(feed)
    feed.tick(price=4298.0)
    for _ in range(6):
        feed.tape.push(4297.0, 4298.0, 4296.0, 4297.5)
        feed.tick(price=4297.5)
    notes = [m for m in feed.messages() if "EVIDENCA PO NDËRTOHET" in m]
    assert 1 <= len(notes) <= 2, "progress must be visible but never spam"


def test_a_paused_engine_still_watches_but_never_says_enter(tmp_path):
    feed = Feed(tmp_path, price=4320.0)
    setup_id = feed.submit(**ZONE)["setup_id"]
    feed.engine.set_paused(True)
    quiet_approach(feed)
    feed.tick(price=4298.0)
    confirm_long(feed)
    feed.tick(price=4301.0)
    assert feed.state(setup_id) == "AT_ZONE"
    assert not any("HYR TANI" in m for m in feed.messages())


def test_status_and_manual_cancel(tmp_path):
    feed = Feed(tmp_path, price=4320.0)
    setup_id = feed.submit(**ZONE)["setup_id"]
    overview = feed.engine.status()
    assert overview["active"][0]["setup_id"] == setup_id
    detail = feed.engine.status(setup_id)
    assert detail["zone"] == [4295.0, 4300.0]
    assert feed.engine.cancel(setup_id)["status"] == "cancelled"
    assert feed.outcome(setup_id) == "MANUAL"
    assert feed.engine.cancel("NOPE")["status"] == "not_found"


def test_stats_count_what_happened(tmp_path):
    feed = Feed(tmp_path, price=4320.0)
    feed.submit(**ZONE)
    setup_id = feed.submit(**ZONE)["setup_id"]
    quiet_approach(feed)
    feed.tick(price=4287.0)
    stats = feed.engine.stats(7)
    assert stats["n"] == 2
    assert stats["cancel"] == 2  # both setups shared the same zone and stop
    assert feed.outcome(setup_id) == "CANCELLED_SL_FIRST"


@pytest.mark.parametrize("price", [4298.0, 4299.9])
def test_the_touch_is_the_whole_zone_not_a_single_price(tmp_path, price):
    feed = Feed(tmp_path, price=4320.0)
    setup_id = feed.submit(**ZONE)["setup_id"]
    quiet_approach(feed)
    feed.tick(price=price)
    assert feed.state(setup_id) == "AT_ZONE"
