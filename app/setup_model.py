"""The `setup_submit` payload (validation-rules §2) and its normalisation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

from app.config import ENTRY_MODELS, PDA_TYPES, TIMEFRAMES

HTF_TIMEFRAMES = ("D1", "H4", "H1")
LTF_TIMEFRAMES = ("M1", "M5", "M15")
PDA_TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")
INVALIDATION_TIMEFRAMES = ("M1", "M5", "M15", "H1")
DIRECTIONS = ("LONG", "SHORT")
BIASES = ("BULLISH", "BEARISH")

MAX_RATIONALE = 600


@dataclass
class SetupInput:
    symbol: str
    direction: str
    entry_model: str
    htf_timeframe: str
    htf_bias: str
    ltf: str
    entry_low: float
    entry_high: float
    stop_loss: float
    tp1: float
    invalidation_level: float
    pda_type: str
    pda_timeframe: str
    pda_low: float
    pda_high: float
    pda_formed_at_ny: str
    opposing_liquidity_level: float
    opposing_liquidity_taken: bool
    price_at_analysis: float
    checklist_positive: int
    checklist_negative: int
    confidence: float
    rationale: str
    tp2: float | None = None
    tp3: float | None = None
    invalidation_timeframe: str | None = None
    pda_mean_threshold: float | None = None
    range_high: float | None = None
    range_low: float | None = None
    model_ref_level: float | None = None
    kill_zone: str | None = None
    macro: str | None = None
    valid_until_ny: str | None = None
    client_ref: str | None = None
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ views
    @property
    def is_long(self) -> bool:
        return self.direction == "LONG"

    @property
    def sign(self) -> int:
        """+1 for LONG, -1 for SHORT. `sign * price` makes every comparison a `>`."""
        return 1 if self.is_long else -1

    @property
    def side(self) -> str:
        return "BUY" if self.is_long else "SELL"

    @property
    def entry_ref(self) -> float:
        return (self.entry_low + self.entry_high) / 2.0

    @property
    def pda_ce(self) -> float:
        return (self.pda_low + self.pda_high) / 2.0

    @property
    def primary_tp(self) -> float:
        return self.tp2 if self.tp2 is not None else self.tp1

    @property
    def targets(self) -> list[float]:
        return [t for t in (self.tp1, self.tp2, self.tp3) if t is not None]

    @property
    def respect_tf(self) -> str:
        """The lower of pda_timeframe and M15 (validation-rules §1)."""
        return self.pda_timeframe if TIMEFRAMES[self.pda_timeframe] <= TIMEFRAMES["M15"] else "M15"

    @property
    def invalidation_tf(self) -> str:
        return self.invalidation_timeframe or self.ltf

    @property
    def timeframes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(("M1", self.ltf, self.respect_tf, self.invalidation_tf, self.pda_timeframe)))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_FIELD_NAMES = {f.name for f in fields(SetupInput)}


def normalise(raw: dict[str, Any]) -> SetupInput:
    """Build a `SetupInput`, swapping inverted zones and recording warnings.

    Raises `ValueError` only for structurally impossible input; every semantic problem is a G-rule.
    """
    data = {k: v for k, v in raw.items() if k in _FIELD_NAMES and v is not None}
    warnings: list[str] = []

    for key in ("symbol", "direction", "entry_model", "htf_timeframe", "htf_bias", "ltf", "pda_type", "pda_timeframe"):
        if key in data and isinstance(data[key], str):
            data[key] = data[key].strip().upper()
    if "invalidation_timeframe" in data and isinstance(data["invalidation_timeframe"], str):
        data["invalidation_timeframe"] = data["invalidation_timeframe"].strip().upper() or None

    for key in (
        "entry_low",
        "entry_high",
        "stop_loss",
        "tp1",
        "tp2",
        "tp3",
        "invalidation_level",
        "pda_low",
        "pda_high",
        "pda_mean_threshold",
        "range_high",
        "range_low",
        "model_ref_level",
        "opposing_liquidity_level",
        "price_at_analysis",
        "confidence",
    ):
        if key in data:
            try:
                data[key] = float(data[key])
            except (TypeError, ValueError):
                data[key] = float("nan")
    for key in ("checklist_positive", "checklist_negative"):
        if key in data:
            try:
                data[key] = int(data[key])
            except (TypeError, ValueError):
                data[key] = -1
    if "opposing_liquidity_taken" in data:
        data["opposing_liquidity_taken"] = bool(data["opposing_liquidity_taken"])

    try:
        setup = SetupInput(**data)
    except TypeError as exc:  # missing required fields → G-01 handles the message
        raise ValueError(str(exc)) from exc

    if setup.entry_low > setup.entry_high:
        setup.entry_low, setup.entry_high = setup.entry_high, setup.entry_low
        warnings.append("entry_low/entry_high u ndërruan")
    if setup.pda_low > setup.pda_high:
        setup.pda_low, setup.pda_high = setup.pda_high, setup.pda_low
        warnings.append("pda_low/pda_high u ndërruan")
    if setup.range_low is not None and setup.range_high is not None and setup.range_low > setup.range_high:
        setup.range_low, setup.range_high = setup.range_high, setup.range_low
        warnings.append("range_low/range_high u ndërruan")
    if setup.rationale and len(setup.rationale) > MAX_RATIONALE:
        setup.rationale = setup.rationale[:MAX_RATIONALE]
        warnings.append("rationale u shkurtua")
    setup.warnings = warnings
    return setup


def enum_problems(setup: SetupInput) -> list[str]:
    """Enum / range problems reported by G-01."""
    problems: list[str] = []
    if setup.direction not in DIRECTIONS:
        problems.append("direction")
    if setup.entry_model not in ENTRY_MODELS:
        problems.append("entry_model")
    if setup.htf_timeframe not in HTF_TIMEFRAMES:
        problems.append("htf_timeframe")
    if setup.htf_bias not in BIASES:
        problems.append("htf_bias")
    if setup.ltf not in LTF_TIMEFRAMES:
        problems.append("ltf")
    if setup.pda_type not in PDA_TYPES:
        problems.append("pda_type")
    if setup.pda_timeframe not in PDA_TIMEFRAMES:
        problems.append("pda_timeframe")
    if setup.invalidation_timeframe and setup.invalidation_timeframe not in INVALIDATION_TIMEFRAMES:
        problems.append("invalidation_timeframe")
    if not (0 <= setup.confidence <= 100):
        problems.append("confidence")
    if setup.checklist_positive < 0 or setup.checklist_negative < 0:
        problems.append("checklist")
    return problems
