"""The no-rejection contract: whatever arrives becomes a watchable setup (docs/VALIDATOR.md §1.2)."""

from __future__ import annotations

import pytest

from app.setup_model import LONG, SHORT, UnusableSetup, normalise


def test_a_single_entry_price_becomes_a_zone():
    setup = normalise({"symbol": "xauusd", "entry": 4300.0, "stop_loss": 4290.0}, zone_pad=0.5)
    assert setup.symbol == "XAUUSD"
    assert (setup.zone_low, setup.zone_high) == (4299.5, 4300.5)
    assert setup.direction == LONG
    assert "zona u ndërtua" in " ".join(setup.notes)


def test_the_direction_is_read_from_the_stop_when_it_is_not_given():
    assert normalise({"entry": 4300.0, "stop_loss": 4310.0}).direction == SHORT
    assert normalise({"entry": 4300.0, "stop_loss": 4290.0}).direction == LONG


def test_a_direction_that_contradicts_the_numbers_is_corrected_not_refused():
    setup = normalise({"direction": "LONG", "entry": 4300.0, "stop_loss": 4310.0, "tp1": 4280.0})
    assert setup.direction == SHORT
    assert any("nuk përputhet" in note for note in setup.notes)
    assert setup.targets == [4280.0]


def test_missing_targets_are_computed_at_1r_2r_3r():
    setup = normalise({"entry_low": 4295.0, "entry_high": 4305.0, "stop_loss": 4290.0})
    assert setup.risk == pytest.approx(10.0)
    assert setup.targets == pytest.approx([4310.0, 4320.0, 4330.0])
    assert any("1R, 2R, 3R" in note for note in setup.notes)


def test_targets_on_the_wrong_side_are_dropped_and_the_rest_kept():
    setup = normalise({"entry": 4300.0, "stop_loss": 4290.0, "tp1": 4280.0, "tp2": 4320.0})
    assert setup.targets == [4320.0]
    assert any("anën e gabuar" in note for note in setup.notes)


def test_an_inverted_zone_is_swapped():
    setup = normalise({"entry_low": 4305.0, "entry_high": 4295.0, "stop_loss": 4290.0})
    assert (setup.zone_low, setup.zone_high) == (4295.0, 4305.0)
    assert any("përmbysur" in note for note in setup.notes)


def test_a_stop_inside_the_zone_is_pushed_out_instead_of_refused():
    setup = normalise({"entry_low": 4295.0, "entry_high": 4305.0, "stop_loss": 4300.0, "tp1": 4330.0})
    assert setup.stop_loss < setup.zone_low
    assert any("brenda zonës" in note for note in setup.notes)


def test_only_a_payload_without_numbers_is_unusable():
    with pytest.raises(UnusableSetup):
        normalise({"symbol": "XAUUSD", "note": "blej arin"})


def test_a_setup_survives_a_round_trip_through_the_database():
    from app.setup_model import Setup

    setup = normalise({"entry": 4300.0, "stop_loss": 4290.0, "tp1": 4320.0, "label": "breakout"})
    assert Setup.from_dict(setup.as_dict()) == setup
