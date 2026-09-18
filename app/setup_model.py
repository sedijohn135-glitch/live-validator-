"""The universal setup: whatever a prompt produced, turned into numbers the validator can watch.

Nothing here rejects. Every repair is recorded in `notes` so the owner sees what was changed and
why, but a submission always comes out the other side as a setup. See docs/VALIDATOR.md §1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LONG = "LONG"
SHORT = "SHORT"

# Only a payload without a usable stop or entry cannot become a setup.
class UnusableSetup(ValueError):
    """Neither an entry nor a stop could be read from the payload."""


@dataclass
class Setup:
    symbol: str
    direction: str
    zone_low: float
    zone_high: float
    stop_loss: float
    targets: list[float] = field(default_factory=list)
    label: str = ""
    note: str = ""
    client_ref: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def is_long(self) -> bool:
        return self.direction == LONG

    @property
    def entry_edge(self) -> float:
        """The edge price meets first: the far side of the zone is where it arrives from."""
        return self.zone_high if self.is_long else self.zone_low

    @property
    def zone_mid(self) -> float:
        return (self.zone_low + self.zone_high) / 2

    @property
    def risk(self) -> float:
        """R of the original idea, measured from the zone mid."""
        return abs(self.zone_mid - self.stop_loss)

    @property
    def tp1(self) -> float | None:
        return self.targets[0] if self.targets else None

    def ahead(self, price: float, level: float) -> bool:
        """True when `level` is further along the trade direction than `price`."""
        return level > price if self.is_long else level < price

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "zone_low": self.zone_low,
            "zone_high": self.zone_high,
            "stop_loss": self.stop_loss,
            "targets": list(self.targets),
            "label": self.label,
            "note": self.note,
            "client_ref": self.client_ref,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Setup:
        return cls(
            symbol=raw["symbol"],
            direction=raw["direction"],
            zone_low=float(raw["zone_low"]),
            zone_high=float(raw["zone_high"]),
            stop_loss=float(raw["stop_loss"]),
            targets=[float(t) for t in raw.get("targets", [])],
            label=raw.get("label", ""),
            note=raw.get("note", ""),
            client_ref=raw.get("client_ref", ""),
            notes=list(raw.get("notes", [])),
        )


def _number(raw: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = raw.get(key)
        if value is None or value == "":
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number != 0.0:
            return number
    return None


def normalise(raw: dict[str, Any], zone_pad: float = 0.0) -> Setup:
    """Turn any payload into a watchable setup. `zone_pad` widens a single-price entry."""
    symbol = str(raw.get("symbol") or "XAUUSD").upper()
    low = _number(raw, "zone_low", "entry_low")
    high = _number(raw, "zone_high", "entry_high")
    single = _number(raw, "entry", "entry_price", "price")
    stop = _number(raw, "stop_loss", "sl", "stop")
    notes: list[str] = []

    if low is None and high is None and single is None and stop is None:
        raise UnusableSetup("as çmim hyrjeje as stop nuk u lexuan")

    if low is None or high is None:
        anchor = single if single is not None else (low if low is not None else high)
        if anchor is None:
            raise UnusableSetup("çmimi i hyrjes mungon")
        pad = abs(zone_pad)
        low, high = anchor - pad, anchor + pad
        if pad:
            notes.append("zona u ndërtua rreth çmimit të hyrjes")
    if low > high:
        low, high = high, low
        notes.append("kufijtë e zonës ishin të përmbysur")

    direction = _direction(raw, low, high, stop, notes)
    stop = _repair_stop(direction, low, high, stop, notes)
    targets = _targets(raw, direction, (low + high) / 2, stop, notes)

    return Setup(
        symbol=symbol,
        direction=direction,
        zone_low=low,
        zone_high=high,
        stop_loss=stop,
        targets=targets,
        label=str(raw.get("label") or raw.get("entry_model") or "")[:40],
        note=str(raw.get("note") or raw.get("rationale") or "")[:300],
        client_ref=str(raw.get("client_ref") or "")[:60],
        notes=notes,
    )


def _direction(raw: dict[str, Any], low: float, high: float, stop: float | None, notes: list[str]) -> str:
    """Geometry decides. A word that contradicts the numbers is a typo, not an instruction."""
    word = str(raw.get("direction") or "").strip().upper()
    stated = LONG if word in ("LONG", "BUY", "BULL", "BULLISH") else ""
    if not stated and word in ("SHORT", "SELL", "BEAR", "BEARISH"):
        stated = SHORT
    geometric = ""
    if stop is not None and (stop < low or stop > high):
        geometric = LONG if stop < low else SHORT
    else:
        # A stop inside the zone says nothing about the side; the first target does.
        target = _number(raw, "tp1", "tp2", "tp3")
        if target is not None and not low <= target <= high:
            geometric = LONG if target > high else SHORT
    if stated and geometric and stated != geometric:
        notes.append(f"drejtimi i shkruar ({stated.lower()}) nuk përputhet me stopin — u mor {geometric.lower()}")
        return geometric
    return stated or geometric or LONG


def _repair_stop(direction: str, low: float, high: float, stop: float | None, notes: list[str]) -> float:
    """A stop on the wrong side is pushed just past the far edge of the zone."""
    span = max(high - low, 0.0)
    fallback = max(span, abs(high) * 0.001)
    if direction == LONG:
        if stop is None or stop >= low:
            notes.append("stopi mungonte ose ishte brenda zonës — u vendos nën zonë")
            return low - fallback
        return stop
    if stop is None or stop <= high:
        notes.append("stopi mungonte ose ishte brenda zonës — u vendos mbi zonë")
        return high + fallback
    return stop


def _targets(raw: dict[str, Any], direction: str, mid: float, stop: float, notes: list[str]) -> list[float]:
    given = [_number(raw, key) for key in ("tp1", "tp2", "tp3")]
    extra = raw.get("targets")
    if isinstance(extra, (list, tuple)):
        given.extend(_number({"t": value}, "t") for value in extra)
    risk = abs(mid - stop)
    sign = 1.0 if direction == LONG else -1.0
    kept: list[float] = []
    dropped = 0
    for value in given:
        if value is None:
            continue
        if (value - mid) * sign <= 0:
            dropped += 1
            continue
        kept.append(value)
    kept = sorted(set(kept), key=lambda v: (v - mid) * sign)
    if dropped:
        notes.append(f"{dropped} objektiv(a) ishin në anën e gabuar — u hoqën")
    if not kept and risk > 0:
        kept = [mid + sign * risk * multiple for multiple in (1.0, 2.0, 3.0)]
        notes.append("objektivat mungonin — u llogaritën te 1R, 2R, 3R")
    return kept[:3]
