"""Intake gate (G-01…G-20), hard invalidation (L-01…L-04), triggers (T-01…T-10) and the score.

Every rule is a pure function over an injected `MarketContext` and returns a `RuleResult`
(validation-rules §0). The gate evaluates all rules and reports every failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.config import FVG_FAMILY, TIMEFRAMES
from app.context import MarketContext
from app.market import (
    FVG,
    Candle,
    displacement,
    efficiency_ratio,
    find_fvgs,
    swing_high_indices,
    swing_low_indices,
)
from app.setup_model import SetupInput, enum_problems
from app.timeutil import (
    active_macro,
    daily_close,
    friday_close,
    friday_cutoff_reached,
    from_epoch,
    in_lunch,
    in_model_windows,
    is_market_open,
    model_windows,
    next_window_end,
    ny_string,
    parse_ny,
    to_ny,
)

SESSION_LEVEL_KEYS = (
    "asian_high",
    "asian_low",
    "london_high",
    "london_low",
    "pdh",
    "pdl",
    "pwh",
    "pwl",
    "ny_midnight_open",
    "six_am_open",
    "lookback_high",
    "lookback_low",
)

LIQUIDITY_SWING_TIMEFRAMES = ("M5", "M15", "H1", "H4", "D1")


@dataclass
class RuleResult:
    code: str
    passed: bool
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def ok(code: str, **data: Any) -> RuleResult:
    return RuleResult(code, True, "", data)


def fail(code: str, text: str, **data: Any) -> RuleResult:
    return RuleResult(code, False, text, data)


def fmt(value: float | None, decimals: int) -> str:
    return "-" if value is None else f"{value:.{decimals}f}"


# --------------------------------------------------------------------- helpers
def beyond(price: float, level: float, sign: int) -> bool:
    """True when `price` is on the losing side of `level` for a trade of this direction."""
    return (price - level) * sign < 0


def hard_level(setup: SetupInput, ctx: MarketContext) -> tuple[float, str]:
    """L-03 hard level: the price whose body close kills the setup."""
    sign = setup.sign
    near = setup.pda_low if setup.is_long else setup.pda_high
    ce = setup.pda_ce
    model = setup.entry_model
    if model in ("OR_FIRST_FVG", "OR_PM_FIRST_FVG", "LUNCH_MACRO_PM", "MARKET_ANCHOR"):
        return ce, "CE"
    if model == "SILVER_BULLET":
        return near, "skaji i FVG-së"
    kind = setup.pda_type
    if kind in ("INVERSION_FVG", "SUSPENSION_BLOCK"):
        return ce, "CE"
    if kind in ("ORDER_BLOCK", "MITIGATION_BLOCK"):
        return (setup.pda_mean_threshold, "mean threshold") if setup.pda_mean_threshold is not None else (ce, "CE")
    if kind in ("BREAKER_BLOCK", "REJECTION_BLOCK"):
        return near, "skaji i bllokut"
    if kind == "OLD_HIGH_LOW":
        return near - sign * ctx.tol(setup.respect_tf), "niveli i vjetër"
    # FVG, LIQUIDITY_VOID, BPR, OTE_ZONE
    return (ce, "CE") if ctx.profile.ce_hard_fvg else (near, "skaji i FVG-së")


def zones_match(low_a: float, high_a: float, low_b: float, high_b: float, tol: float) -> bool:
    """Both edges within 2×tol, or ≥ 60 % overlap of the narrower zone (G-13)."""
    if abs(low_a - low_b) <= 2 * tol and abs(high_a - high_b) <= 2 * tol:
        return True
    overlap = min(high_a, high_b) - max(low_a, low_b)
    narrower = min(high_a - low_a, high_b - low_b)
    if narrower <= 0:
        return False
    return overlap / narrower >= 0.6


def _candles_around(candles: list[Candle], formed_ts: float, tf_seconds: int, bars: int = 3) -> list[int]:
    span = bars * tf_seconds
    return [i for i, c in enumerate(candles) if abs(c.t - formed_ts) <= span]


def verify_pda(setup: SetupInput, ctx: MarketContext, formed_ts: float) -> tuple[str, str]:
    """G-13. Returns `(VERIFIED | NOT_FOUND | UNVERIFIED, detail)`."""
    kind = setup.pda_type
    if kind in ("BREAKER_BLOCK", "REJECTION_BLOCK", "OTE_ZONE"):
        return "UNVERIFIED", "lloji i PDA-së s'verifikohet nga të dhënat"

    tf = setup.pda_timeframe
    candles = ctx.candles(tf)
    if len(candles) < 3:
        return "NOT_FOUND", "s'ka mjaftueshëm qirinj"
    tf_seconds = TIMEFRAMES[tf]
    tol = ctx.tol(tf)
    window = _candles_around(candles, formed_ts, tf_seconds)
    if not window:
        return "NOT_FOUND", "koha e formimit nuk gjendet në të dhëna"
    direction = "BULL" if setup.is_long else "BEAR"
    opposite = "BEAR" if setup.is_long else "BULL"
    fvgs = find_fvgs(candles)

    if kind == "OLD_HIGH_LOW":
        indices = swing_low_indices(candles) if setup.is_long else swing_high_indices(candles)
        far = setup.pda_low if setup.is_long else setup.pda_high
        for i in indices:
            price = candles[i].l if setup.is_long else candles[i].h
            if abs(price - far) <= 2 * tol:
                return "VERIFIED", f"swing te {price}"
        return "NOT_FOUND", "s'u gjet swing te niveli"

    if kind in ("ORDER_BLOCK", "MITIGATION_BLOCK"):
        for i in window:
            candle = candles[i]
            is_opposite = candle.bearish if setup.is_long else candle.bullish
            if not is_opposite:
                continue
            matches = zones_match(candle.l, candle.h, setup.pda_low, setup.pda_high, tol) or zones_match(
                candle.body_bot, candle.body_top, setup.pda_low, setup.pda_high, tol
            )
            if not matches:
                continue
            extreme = candle.h if setup.is_long else candle.l
            for j in range(i + 1, min(i + 6, len(candles))):
                closed_beyond = candles[j].c > extreme if setup.is_long else candles[j].c < extreme
                if not closed_beyond:
                    continue
                if any(f.direction == direction and i <= f.index <= j for f in fvgs):
                    return "VERIFIED", f"OB te {candle.t}"
            return "NOT_FOUND", "blloku s'u pasua nga mbyllje + FVG"
        return "NOT_FOUND", "s'u gjet order block"

    if kind in ("INVERSION_FVG", "SUSPENSION_BLOCK"):
        for f in fvgs:
            if f.direction != opposite:
                continue
            if not zones_match(f.low, f.high, setup.pda_low, setup.pda_high, tol):
                continue
            if setup.entry_model == "LUNCH_MACRO_PM":
                return "VERIFIED", "inversion (PM, kërkesa e mbylljes hiqet)"
            far = f.high if setup.is_long else f.low
            atr = ctx.atr(tf)
            for j in range(f.index + 2, len(candles)):
                broke = candles[j].c > far if setup.is_long else candles[j].c < far
                if broke and displacement(
                    candles[j], atr or 0.0, ctx.profile.disp_body_atr, ctx.profile.disp_body_range
                ):
                    return "VERIFIED", f"inversion te {ny_string(candles[j].t)}"
            return "NOT_FOUND", "FVG-ja nuk u invertua me displacement"
        return "NOT_FOUND", "s'u gjet FVG e kundërt për inversion"

    # FVG / LIQUIDITY_VOID / BPR
    needs_displacement = setup.entry_model in ("OR_FIRST_FVG", "OR_PM_FIRST_FVG")
    atr = ctx.atr(tf)
    for f in fvgs:
        if f.direction != direction or f.index not in window:
            continue
        if not zones_match(f.low, f.high, setup.pda_low, setup.pda_high, tol):
            continue
        if needs_displacement and not displacement(
            candles[f.index], atr or 0.0, ctx.profile.disp_body_atr, ctx.profile.disp_body_range
        ):
            return "NOT_FOUND", "qiriu i mesit s'ka displacement"
        return "VERIFIED", f"FVG {f.kind} {f.low:.5f}-{f.high:.5f}"
    return "NOT_FOUND", "FVG-ja e deklaruar nuk ekziston në të dhëna"


def liquidity_is_real(setup: SetupInput, ctx: MarketContext) -> bool:
    """G-15 reality: a strict swing on M5/M15/H1 in the last 5 days, or a session level."""
    level = setup.opposing_liquidity_level
    tol = 2 * ctx.tol("M15")
    horizon = ctx.now_ts - 5 * 86400
    for tf in LIQUIDITY_SWING_TIMEFRAMES:
        # Daily swings carry no 5-day horizon: a monthly high is still the pool being run.
        candles = ctx.candles(tf) if tf in ("H4", "D1") else [c for c in ctx.candles(tf) if c.t >= horizon]
        indices = swing_low_indices(candles) if setup.is_long else swing_high_indices(candles)
        for i in indices:
            price = candles[i].l if setup.is_long else candles[i].h
            if abs(price - level) <= tol:
                return True
    for key in SESSION_LEVEL_KEYS:
        value = ctx.level(key)
        if value is not None and abs(value - level) <= tol:
            return True
    return False


def liquidity_taken(setup: SetupInput, ctx: MarketContext, window_h: float, until_ts: float | None = None) -> bool:
    """True when price traded through the opposing liquidity level inside the window (T-01)."""
    end = ctx.now_ts if until_ts is None else until_ts
    start = end - window_h * 3600
    level = setup.opposing_liquidity_level
    for tf in ("M1", "M5"):
        for candle in ctx.candles(tf):
            if not (start <= candle.t <= end):
                continue
            if setup.is_long and candle.l <= level:
                return True
            if not setup.is_long and candle.h >= level:
                return True
    return False


# ------------------------------------------------------------------ intake gate
@dataclass
class IntakeResult:
    passed: bool
    failures: list[RuleResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    computed: dict[str, Any] = field(default_factory=dict)


def g01_schema(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    problems = enum_problems(setup)
    prices = [
        setup.entry_low,
        setup.entry_high,
        setup.stop_loss,
        setup.tp1,
        setup.invalidation_level,
        setup.pda_low,
        setup.pda_high,
        setup.opposing_liquidity_level,
        setup.price_at_analysis,
    ] + [t for t in (setup.tp2, setup.tp3) if t is not None]
    if any(not (p == p) or p <= 0 or p == float("inf") for p in prices):  # NaN / non-positive
        problems.append("çmime")
    try:
        parse_ny(setup.pda_formed_at_ny)
    except (ValueError, TypeError):
        problems.append("pda_formed_at_ny")
    if setup.valid_until_ny:
        try:
            parse_ny(setup.valid_until_ny)
        except (ValueError, TypeError):
            problems.append("valid_until_ny")
    if problems:
        return fail("G-01", "Fusha të pavlefshme: " + ", ".join(sorted(set(problems))), problems=problems)
    return ok("G-01")


def g02_data(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    """Never arm blind — but a tick-feed hiccup is not blindness while the candles are fresh.

    Arming only starts monitoring, so a candle close standing in for the tick is enough here. The
    ENTER decision is a different matter: T-07 demands a real quote no older than five seconds.
    """
    if ctx.bid is None:
        return fail("G-02", "Të dhënat mungojnë (çmimi live)", quote_age=ctx.quote_age)
    if ctx.quote_age > ctx.profile.intake_quote_max_age_s:
        return fail("G-02", "Të dhënat mungojnë (çmimi live)", quote_age=ctx.quote_age)
    needed = set(setup.timeframes) | {"H1", "D1"}
    missing = [tf for tf in sorted(needed) if not ctx.has_candles(tf, 15)]
    if missing:
        return fail("G-02", "Të dhënat mungojnë: " + ", ".join(missing), missing=missing)
    return ok("G-02")


def g03_bias(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    expected = "BULLISH" if setup.is_long else "BEARISH"
    if setup.htf_bias != expected:
        return fail("G-03", "Drejtimi nuk përputhet me bias-in HTF")
    return ok("G-03")


def g04_ordering(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    sign = setup.sign
    chain = [setup.stop_loss, setup.entry_low, setup.entry_high] if setup.is_long else [
        setup.stop_loss,
        setup.entry_high,
        setup.entry_low,
    ]
    chain += setup.targets
    for a, b in zip(chain, chain[1:], strict=False):
        if (b - a) * sign <= 0:
            return fail("G-04", "Nivelet janë në rend të gabuar")
    tol = ctx.tol(setup.pda_timeframe)
    if setup.entry_low > setup.pda_high + tol or setup.entry_high < setup.pda_low - tol:
        return fail("G-04", "Zona e hyrjes nuk prek PDA-në")
    return ok("G-04")


def g05_invalidation(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    if setup.is_long:
        placed = setup.stop_loss <= setup.invalidation_level <= setup.entry_high
    else:
        placed = setup.entry_low <= setup.invalidation_level <= setup.stop_loss
    if not placed:
        return fail("G-05", "Niveli i invalidimit është jashtë vendit")
    return ok("G-05")


def g06_drift(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    atr_h1 = ctx.atr("H1") or 0.0
    allowed = max(ctx.profile.price_drift_atr_h1 * atr_h1, ctx.profile.price_drift_pct / 100.0 * (ctx.bid or 0.0))
    drift = abs(setup.price_at_analysis - (ctx.bid or 0.0))
    if drift > allowed:
        return fail(
            "G-06",
            f"Çmimi i analizës s'përputhet me çmimin live ({fmt(ctx.bid, ctx.sym.display_decimals)})",
            drift=drift,
            allowed=allowed,
        )
    return ok("G-06")


def g07_level_range(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    atr_d1 = ctx.atr("D1") or 0.0
    allowed = ctx.profile.level_range_atr_d1 * atr_d1
    if allowed <= 0:
        return ok("G-07")
    levels = [
        setup.entry_low,
        setup.entry_high,
        setup.stop_loss,
        setup.invalidation_level,
        setup.pda_low,
        setup.pda_high,
        setup.opposing_liquidity_level,
        *setup.targets,
    ]
    worst = max(abs(level - (ctx.bid or 0.0)) for level in levels)
    if worst > allowed:
        return fail("G-07", "Nivele shumë larg çmimit", worst=worst, allowed=allowed)
    return ok("G-07")


def g08_not_invalid(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    sign = setup.sign
    if beyond(ctx.bid or 0.0, setup.stop_loss, sign) or (ctx.bid == setup.stop_loss):
        return fail("G-08", "Setup-i është tashmë i pavlefshëm")
    level, _name = hard_level(setup, ctx)
    respect = ctx.last_closed(setup.respect_tf)
    if respect is not None and beyond(respect.c, level, sign):
        return fail("G-08", "Setup-i është tashmë i pavlefshëm")
    inval = ctx.last_closed(setup.invalidation_tf)
    if inval is not None and beyond(inval.c, setup.invalidation_level, sign):
        return fail("G-08", "Setup-i është tashmë i pavlefshëm")
    return ok("G-08")


def g09_not_late(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    bid = ctx.bid or 0.0
    late = bid >= setup.tp1 if setup.is_long else bid <= setup.tp1
    if late:
        return fail("G-09", "Lëvizja ka ndodhur (çmimi përtej TP1)")
    return ok("G-09")


def g10_rr(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    risk = abs(setup.entry_ref - setup.stop_loss)
    if risk <= 0:
        return fail("G-10", "SL përputhet me hyrjen")
    rr = abs(setup.primary_tp - setup.entry_ref) / risk
    if rr < ctx.profile.rr_min_plan:
        return fail("G-10", f"RR {rr:.2f} &lt; 1:{ctx.profile.rr_min_plan:g}", rr=rr)
    return ok("G-10", rr=rr)


def g11_sl_distance(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    risk = abs(setup.entry_ref - setup.stop_loss)
    atr_ltf = ctx.atr(setup.ltf) or 0.0
    atr_h1 = ctx.atr("H1") or 0.0
    spread = ctx.median_spread()
    if atr_ltf and risk < ctx.profile.sl_min_atr_ltf * atr_ltf:
        return fail("G-11", "SL shumë i ngushtë/i gjerë për volatilitetin", risk=risk, reason="atr_ltf")
    if risk < ctx.profile.spread_sl_mult * spread:
        return fail("G-11", "SL shumë i ngushtë/i gjerë për volatilitetin", risk=risk, reason="spread")
    if atr_h1 and risk > ctx.profile.sl_max_atr_h1 * atr_h1:
        return fail("G-11", "SL shumë i ngushtë/i gjerë për volatilitetin", risk=risk, reason="atr_h1")
    return ok("G-11", risk=risk)


def g12_checklist(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    if setup.checklist_positive < ctx.profile.checklist_min_pos or (
        setup.checklist_negative > ctx.profile.checklist_max_neg
    ):
        return fail(
            "G-12",
            f"Checklist v11: {setup.checklist_positive} pozitive / {setup.checklist_negative} negative",
        )
    return ok("G-12")


def g13_pda(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    try:
        formed = parse_ny(setup.pda_formed_at_ny)
    except (ValueError, TypeError):
        return fail("G-13", "PDA nuk u gjet në të dhëna", status="NOT_FOUND")
    status, detail = verify_pda(setup, ctx, formed.timestamp())
    if status == "NOT_FOUND":
        return fail("G-13", "PDA nuk u gjet në të dhëna", status=status, detail=detail)
    if status == "UNVERIFIED" and ctx.profile.strict_pda_verify:
        return fail("G-13", "PDA nuk u gjet në të dhëna", status=status, detail=detail)
    return ok("G-13", status=status, detail=detail)


def g14_pda_not_failed(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    try:
        formed = parse_ny(setup.pda_formed_at_ny).timestamp()
    except (ValueError, TypeError):
        return ok("G-14")
    level, name = hard_level(setup, ctx)
    tf = setup.respect_tf
    tf_seconds = TIMEFRAMES[tf]
    start = formed + 2 * TIMEFRAMES[setup.pda_timeframe]
    for candle in ctx.candles(tf):
        if candle.t < start:
            continue
        if candle.t + tf_seconds > ctx.now_ts:
            continue
        if beyond(candle.c, level, setup.sign):
            return fail("G-14", "PDA ka dështuar tashmë (trup mbylli përtej)", level=level, level_name=name)
    return ok("G-14")


def g15_liquidity(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    level = setup.opposing_liquidity_level
    tol = ctx.tol("M15")
    # v11 §5.8 only requires that the opposing pool was taken before entry. It is routinely far
    # beyond the stop — a PM continuation shorts an inversion array long after the morning swept the
    # PDH. Placement therefore only checks the side: SSL below a long, BSL above a short. G-07 still
    # keeps every level within 6 x ATR(D1) of price, and the reality and sweep checks below do the work.
    if setup.is_long:
        placed = level <= setup.entry_low + tol
    else:
        placed = level >= setup.entry_high - tol
    if not placed:
        return fail("G-15", "Likuiditeti kundërt s'është real / s'është marrë", reason="placement")
    if not liquidity_is_real(setup, ctx):
        return fail("G-15", "Likuiditeti kundërt s'është real / s'është marrë", reason="not_real")
    if setup.opposing_liquidity_taken and not liquidity_taken(setup, ctx, ctx.profile.liq_window_h):
        return fail("G-15", "Likuiditeti kundërt s'është real / s'është marrë", reason="false_claim")
    return ok("G-15", taken=setup.opposing_liquidity_taken)


def g16_model(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    model = setup.entry_model
    detail = ""
    try:
        formed = parse_ny(setup.pda_formed_at_ny)
    except (ValueError, TypeError):
        formed = None
    atr_d1 = ctx.atr("D1") or 0.0
    bid = ctx.bid or 0.0
    lookback_high = ctx.level("lookback_high")

    if model == "ICT_2022" and setup.pda_type not in FVG_FAMILY:
        detail = "kërkohet PDA e familjes FVG"
    elif model == "MARKET_ANCHOR":
        if not setup.is_long:
            detail = "vetëm LONG"
        elif setup.pda_type != "INVERSION_FVG":
            detail = "kërkohet INVERSION_FVG"
        elif lookback_high is None or lookback_high - bid > ctx.profile.ath_model_prox_atr * atr_d1:
            detail = "s'është pranë ATH"
    elif model == "SM_THREE_STAGE":
        if setup.is_long:
            detail = "vetëm SHORT"
        elif lookback_high is None or lookback_high - bid > ctx.profile.ath_model_prox_atr * atr_d1:
            detail = "s'është pranë ATH"
    elif model in ("TURTLE_SOUP", "TURTLE_SOUP_DEFERRED"):
        if setup.range_high is None or setup.range_low is None:
            detail = "kërkohen range_high/range_low"
        else:
            mid = (setup.range_high + setup.range_low) / 2.0
            if setup.is_long and setup.entry_ref > mid:
                detail = "hyrja s'është në discount"
            elif not setup.is_long and setup.entry_ref < mid:
                detail = "hyrja s'është në premium"
            elif model == "TURTLE_SOUP_DEFERRED" and setup.pda_type not in ("INVERSION_FVG", "ORDER_BLOCK", "FVG"):
                detail = "PDA e papërshtatshme"
    elif model == "SILVER_BULLET":
        windows = model_windows("SILVER_BULLET", formed, ctx.profile.name == "BALANCED")
        if not windows:
            detail = "FVG-ja s'u formua brenda një dritareje Silver Bullet"
        else:
            distance = abs(setup.primary_tp - setup.entry_ref)
            atr_m5 = ctx.atr("M5") or 0.0
            required = max(ctx.sym.sb_min_dol, 1.5 * atr_m5)
            if distance < required:
                detail = "DOL shumë afër"
    elif model == "IOFED" and setup.pda_type not in FVG_FAMILY:
        detail = "kërkohet PDA e familjes FVG"
    elif model == "VENOM":
        if not setup.is_long:
            detail = "vetëm LONG"
        elif setup.model_ref_level is None:
            detail = "kërkohet model_ref_level"
        else:
            detail = _venom_detail(setup, ctx)
    elif model in ("OR_FIRST_FVG", "OR_PM_FIRST_FVG"):
        if not model_windows(model, formed, ctx.profile.name == "BALANCED"):
            detail = "FVG-ja s'u formua në Opening Range"
        elif formed is not None and setup.opposing_liquidity_taken and not liquidity_taken(
            setup, ctx, ctx.profile.liq_window_h, until_ts=formed.timestamp()
        ):
            detail = "likuiditeti s'u mor para formimit"
    elif model == "MODEL_2":
        windows = model_windows("MODEL_2", formed, ctx.profile.name == "BALANCED")
        if not windows:
            detail = "s'ka dritare Model 2"

    if detail:
        return fail("G-16", f"Kushtet e modelit {model} s'plotësohen: {detail}", detail=detail)
    return ok("G-16")


def _venom_detail(setup: SetupInput, ctx: MarketContext) -> str:
    """Venom (v11 §5.11): a SIBI followed within 8 candles by a BISI closing at `model_ref_level`."""
    tf = setup.pda_timeframe
    candles = ctx.candles(tf)
    tol = 2 * ctx.tol(tf)
    fvgs = find_fvgs(candles)
    ref = setup.model_ref_level or 0.0
    for bear in (f for f in fvgs if f.direction == "BEAR"):
        for bull in (f for f in fvgs if f.direction == "BULL" and 0 < f.index - bear.index <= 8):
            close_price = candles[bull.index].c
            if abs(close_price - ref) > tol:
                continue
            lows = [c.l for c in candles[bear.index : bull.index + 1]]
            if lows and setup.stop_loss < min(lows):
                return ""
            return "SL nuk është nën minimumin mes SIBI dhe BISI"
    return "s'u gjet sekuenca SIBI → BISI te model_ref_level"


def g17_ath_short(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    if setup.is_long or setup.entry_model == "SM_THREE_STAGE":
        return ok("G-17")
    lookback_high = ctx.level("lookback_high")
    atr_d1 = ctx.atr("D1") or 0.0
    if lookback_high is None or atr_d1 <= 0:
        return ok("G-17")
    if lookback_high - (ctx.bid or 0.0) <= ctx.profile.ath_prox_atr_d1 * atr_d1:
        return fail("G-17", "SHORT pranë ATH pa konfirmim institucional")
    return ok("G-17")


def compute_expiry(setup: SetupInput, ctx: MarketContext) -> tuple[float | None, list[str]]:
    """G-18: the moment after which the setup can no longer produce an ENTER."""
    now = from_epoch(ctx.now_ts)
    balanced = ctx.profile.name == "BALANCED"
    try:
        formed = parse_ny(setup.pda_formed_at_ny)
    except (ValueError, TypeError):
        formed = None
    windows = model_windows(setup.entry_model, formed, balanced)
    if not windows:
        return None, []
    lifetime_h = ctx.profile.model_2_lifetime_h if setup.entry_model == "MODEL_2" else ctx.profile.max_setup_lifetime_h
    horizon = now + timedelta(hours=lifetime_h)

    candidates: list[datetime] = [horizon]
    if setup.valid_until_ny:
        try:
            candidates.append(parse_ny(setup.valid_until_ny))
        except (ValueError, TypeError):
            pass
    window_end = next_window_end(windows, now, horizon)
    if window_end is None:
        return None, [w.name for w in windows]
    candidates.append(window_end)
    candidates.append(daily_close(now))
    cutoff = friday_close(now, ctx.profile.friday_cutoff_ny)
    if cutoff is not None and ctx.symbol.upper() == "XAUUSD":
        candidates.append(cutoff)
    expires = min(candidates)
    return expires.timestamp(), [w.name for w in windows]


def g18_time(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    expires_at, window_names = compute_expiry(setup, ctx)
    if expires_at is None or expires_at < ctx.now_ts + 300:
        return fail("G-18", "S'ka Kill Zone të vlefshme para skadimit", windows=window_names)
    return ok("G-18", expires_at=expires_at, windows=window_names)


INTAKE_RULES = (
    g01_schema,
    g02_data,
    g03_bias,
    g04_ordering,
    g05_invalidation,
    g06_drift,
    g07_level_range,
    g08_not_invalid,
    g09_not_late,
    g10_rr,
    g11_sl_distance,
    g12_checklist,
    g13_pda,
    g14_pda_not_failed,
    g15_liquidity,
    g16_model,
    g17_ath_short,
    g18_time,
)

# Rules that must pass before a setup is worth shadow-tracking (validation-rules §11).
SHADOW_MIN_RULES = ("G-01", "G-02", "G-03", "G-04", "G-05", "G-06", "G-07")


def evaluate_intake(setup: SetupInput, ctx: MarketContext) -> IntakeResult:
    """Run every intake rule and collect all failures (validation-rules §3)."""
    results: list[RuleResult] = []
    schema = g01_schema(setup, ctx)
    results.append(schema)
    if schema.passed:
        data = g02_data(setup, ctx)
        results.append(data)
        if data.passed:
            results.extend(rule(setup, ctx) for rule in INTAKE_RULES[2:])
    failures = [r for r in results if not r.passed]
    passed_codes = {r.code for r in results if r.passed}
    computed = _computed(setup, ctx, results)
    warnings = list(setup.warnings)
    pda = next((r for r in results if r.code == "G-13"), None)
    if pda is not None and pda.data.get("status") == "UNVERIFIED":
        warnings.append("PDA e paverifikuar")
    return IntakeResult(
        passed=not failures,
        failures=failures,
        warnings=warnings,
        computed={**computed, "passed_codes": sorted(passed_codes)},
    )


def _computed(setup: SetupInput, ctx: MarketContext, results: list[RuleResult]) -> dict[str, Any]:
    by_code = {r.code: r for r in results}
    risk = abs(setup.entry_ref - setup.stop_loss)
    rr = abs(setup.primary_tp - setup.entry_ref) / risk if risk > 0 else None
    level, level_name = hard_level(setup, ctx)
    expires_at = by_code.get("G-18", RuleResult("G-18", False)).data.get("expires_at")
    windows = by_code.get("G-18", RuleResult("G-18", False)).data.get("windows", [])
    return {
        "ce": setup.pda_ce,
        "entry_ref": setup.entry_ref,
        "rr_plan": round(rr, 2) if rr else None,
        "risk": risk,
        "hard_level": level,
        "hard_level_name": level_name,
        "atr": {tf: ctx.atr(tf) for tf in ("M5", "M15", "H1", "D1")},
        "tol": ctx.tol(setup.respect_tf),
        "median_spread": ctx.median_spread(),
        "pda_check": by_code.get("G-13", RuleResult("G-13", False)).data,
        "liquidity_check": by_code.get("G-15", RuleResult("G-15", False)).data,
        "expires_at": expires_at,
        "expires_at_ny": ny_string(expires_at) if expires_at else None,
        "windows": windows,
        "respect_tf": setup.respect_tf,
        "invalidation_tf": setup.invalidation_tf,
        "waits_for_sq": waits_for_text(setup, ctx),
    }


def waits_for_text(setup: SetupInput, ctx: MarketContext) -> str:
    """Plain Albanian: what must happen before ENTER."""
    if setup.entry_model == "VENOM":
        return f"çmimi ≤ {fmt(setup.model_ref_level, ctx.sym.display_decimals)} (mbyllja BISI)"
    if setup.entry_model == "TURTLE_SOUP_DEFERRED":
        return f"qiri refuzimi (fitil deri CE, trup jashtë zonës) në {setup.ltf}"
    parts = [f"prekje të zonës → CISD në {setup.ltf} + displacement"]
    if not setup.opposing_liquidity_taken:
        parts.append("likuiditeti kundërt duhet të merret")
    if setup.entry_model == "IOFED":
        parts = ["prekje të zonës → FVG e re brenda zonës"]
    return " · ".join(parts)


# ------------------------------------------------------- live invalidation (§6)
def l01_sl_touch(setup: SetupInput, ctx: MarketContext, candle: Candle | None) -> RuleResult:
    sign = setup.sign
    if candle is not None:
        extreme = candle.l if setup.is_long else candle.h
        if (extreme - setup.stop_loss) * sign <= 0:
            return fail("L-01", "SL u prek para hyrjes")
    if ctx.bid is not None and (ctx.bid - setup.stop_loss) * sign <= 0:
        return fail("L-01", "SL u prek para hyrjes")
    return ok("L-01")


def l02_invalidation_close(setup: SetupInput, ctx: MarketContext, candle: Candle) -> RuleResult:
    if beyond(candle.c, setup.invalidation_level, setup.sign):
        return fail(
            "L-02",
            f"Mbyllje {setup.invalidation_tf} përtej invalidimit "
            f"{fmt(setup.invalidation_level, ctx.sym.display_decimals)}",
        )
    return ok("L-02")


def l03_body_respect(setup: SetupInput, ctx: MarketContext, candle: Candle) -> RuleResult:
    level, name = hard_level(setup, ctx)
    if beyond(candle.c, level, setup.sign):
        return fail(
            "L-03",
            f"Trupi mbylli përtej {name} {fmt(level, ctx.sym.display_decimals)} ({setup.respect_tf})",
            level=level,
        )
    return ok("L-03", level=level)


def l04_expiry(expires_at: float | None, now_ts: float) -> RuleResult:
    if expires_at is not None and now_ts >= expires_at:
        return fail("L-04", "skadoi")
    return ok("L-04")


# ------------------------------------------------------------------ triggers (§7)
@dataclass
class TriggerState:
    tap_index: int  # index x of the ltf candle holding the tap extreme
    tap_extreme: float
    tap_ts: float


def find_cisd(candles: list[Candle], x: int, k: int, is_long: bool, lookback: int, mss_lookback: int) -> float | None:
    """T-02: the CISD level, or the MSS fallback level. `None` when neither exists."""
    j = None
    for i in range(x, max(-1, x - lookback - 1), -1):
        if i < 0 or i >= len(candles):
            continue
        if (candles[i].bearish if is_long else candles[i].bullish):
            j = i
            break
    if j is not None:
        start = j
        while start - 1 >= 0 and (candles[start - 1].bearish if is_long else candles[start - 1].bullish):
            start -= 1
        opens = [c.o for c in candles[start : j + 1]]
        return max(opens) if is_long else min(opens)
    indices = swing_high_indices(candles) if is_long else swing_low_indices(candles)
    window = [i for i in indices if max(0, x - mss_lookback) <= i < x]
    if not window:
        return None
    last = window[-1]
    return candles[last].h if is_long else candles[last].l


def t02_cisd(setup: SetupInput, ctx: MarketContext, candles: list[Candle], state: TriggerState, k: int) -> RuleResult:
    if k <= state.tap_index:
        return fail("T-02", "CISD mungon", reason="same_bar")
    level = find_cisd(
        candles, state.tap_index, k, setup.is_long, ctx.profile.cisd_lookback, ctx.profile.mss_lookback
    )
    if level is None:
        return fail("T-02", "CISD mungon", reason="no_level")
    close = candles[k].c
    passed = close > level if setup.is_long else close < level
    return ok("T-02", level=level) if passed else fail("T-02", "CISD mungon", level=level)


def t03_displacement(
    setup: SetupInput, ctx: MarketContext, candles: list[Candle], state: TriggerState, k: int
) -> RuleResult:
    atr = ctx.atr(setup.ltf) or 0.0
    direction = "BULL" if setup.is_long else "BEAR"
    best = 0.0
    for i in range(state.tap_index + 1, k + 1):
        candle = candles[i]
        aligned = candle.bullish if setup.is_long else candle.bearish
        if aligned:
            best = max(best, candle.body)
            if displacement(candle, atr, ctx.profile.disp_body_atr, ctx.profile.disp_body_range):
                return ok("T-03", body=candle.body, atr=atr)
    for f in find_fvgs(candles):
        if f.direction == direction and f.kind == "wick" and state.tap_index <= f.index and f.index + 1 <= k:
            return ok("T-03", fvg=True, body=best, atr=atr)
    return fail("T-03", "displacement mungon", body=best, atr=atr)


def t04_freshness(ctx: MarketContext, state: TriggerState, k: int) -> RuleResult:
    if k - state.tap_index > ctx.profile.confirm_max_bars:
        return fail("T-04", "konfirmimi erdhi shumë vonë pas prekjes")
    return ok("T-04")


def global_block(setup: SetupInput, ctx: MarketContext, moment: datetime) -> str | None:
    """Global blocks checked at every trigger (validation-rules §4)."""
    if in_lunch(moment):
        return "dreka (12:00–13:00 NY)"
    if not is_market_open(ctx.symbol, moment):
        return "tregu është i mbyllur"
    if friday_cutoff_reached(ctx.symbol, moment, ctx.profile.friday_cutoff_ny):
        return "e premte pas kufirit"
    if ctx.news_blackout:
        return f"lajme: {ctx.news_blackout}"
    if ctx.paused:
        return "të dhënat në pauzë"
    return None


def t05_time(setup: SetupInput, ctx: MarketContext, moment: datetime, btc_weekend: bool) -> RuleResult:
    if ctx.symbol.upper() == "BTCUSD" and to_ny(moment).weekday() >= 5 and not btc_weekend:
        return fail("T-05", "fundjavë (BTC)")
    blocked = global_block(setup, ctx, moment)
    if blocked:
        return fail("T-05", blocked)
    try:
        formed = parse_ny(setup.pda_formed_at_ny)
    except (ValueError, TypeError):
        formed = None
    window = in_model_windows(setup.entry_model, formed, ctx.profile.name == "BALANCED", moment)
    if window is None:
        return fail("T-05", "jashtë Kill Zone-s së modelit")
    return ok("T-05", window=window)


def t06_spread(ctx: MarketContext) -> RuleResult:
    spread = ctx.spread
    if spread is None:
        return fail("T-06", "spread i panjohur")
    limit = min(ctx.sym.max_spread_abs, ctx.profile.spread_spike_mult * ctx.median_spread())
    if spread > limit:
        return fail("T-06", f"spread i lartë ({fmt(spread, ctx.sym.display_decimals)})", spread=spread, limit=limit)
    return ok("T-06", spread=spread, limit=limit)


def chase_limit(setup: SetupInput, rr_min_trigger: float, chase_frac: float) -> float:
    """T-07: the furthest entry price that still respects both the chase cap and the RR floor."""
    zone_edge = setup.entry_high if setup.is_long else setup.entry_low
    risk_edge = abs(zone_edge - setup.stop_loss)
    cap = zone_edge + setup.sign * chase_frac * risk_edge
    rr_cap = (setup.primary_tp + rr_min_trigger * setup.stop_loss) / (1 + rr_min_trigger)
    return min(cap, rr_cap) if setup.is_long else max(cap, rr_cap)


def t07_price(setup: SetupInput, ctx: MarketContext) -> RuleResult:
    if ctx.quote_synthetic:
        # A candle close is a stand-in for arming, never a price to enter at.
        return fail("T-07", "çmimi live mungon (vetëm qiri)", synthetic=True)
    if ctx.quote_age > ctx.profile.quote_max_age_s:
        return fail("T-07", "çmimi live është i vjetër", age=ctx.quote_age)
    entry = ctx.ask if setup.is_long else ctx.bid
    if entry is None:
        return fail("T-07", "çmimi live mungon")
    limit = chase_limit(setup, ctx.profile.rr_min_trigger, ctx.profile.chase_max_risk_frac)
    too_far = entry > limit if setup.is_long else entry < limit
    if too_far:
        return fail(
            "T-07",
            f"çmimi iku përtej {fmt(limit, ctx.sym.display_decimals)}",
            entry=entry,
            chase_limit=limit,
        )
    spread = ctx.spread or 0.0
    if abs(entry - setup.stop_loss) < ctx.profile.spread_sl_mult * spread:
        return fail("T-07", "SL shumë afër për spread-in aktual", entry=entry, chase_limit=limit)
    return ok("T-07", entry=entry, chase_limit=limit)


def t08_data(ctx: MarketContext, setup: SetupInput, state: TriggerState, k_open_ts: float) -> RuleResult:
    if ctx.paused or not ctx.data_ok:
        return fail("T-08", "të dhënat në pauzë")
    missing = ctx.store.missing_between(ctx.symbol, setup.ltf, state.tap_ts, k_open_ts)
    if missing:
        return fail("T-08", "mungojnë qirinj", missing=missing)
    return ok("T-08")


# ---------------------------------------------------------------------- score (§9)
def compute_score(
    setup: SetupInput,
    ctx: MarketContext,
    candles: list[Candle],
    state: TriggerState,
    k: int,
    moment: datetime,
    holiday_today: bool = False,
    pda_unverified: bool = False,
) -> tuple[int, list[str]]:
    breakdown: list[str] = []
    total = 0
    atr_ltf = ctx.atr(setup.ltf) or 0.0

    if setup.range_high is not None and setup.range_low is not None:
        mid = (setup.range_high + setup.range_low) / 2.0
        good = setup.entry_ref <= mid if setup.is_long else setup.entry_ref >= mid
        if good:
            total += 1
            breakdown.append("S-1 discount/premium")
    if active_macro(moment):
        total += 1
        breakdown.append("S-2 macro")
    bodies = [
        c.body
        for c in candles[state.tap_index + 1 : k + 1]
        if (c.bullish if setup.is_long else c.bearish)
    ]
    if atr_ltf and bodies and max(bodies) >= 1.5 * atr_ltf:
        total += 1
        breakdown.append("S-3 displacement i fortë")
    shallow = state.tap_extreme >= setup.pda_ce if setup.is_long else state.tap_extreme <= setup.pda_ce
    if shallow:
        total += 1
        breakdown.append("S-4 prekje e cekët")
    if _session_sweep(setup, ctx, candles[k].t):
        total += 1
        breakdown.append("S-5 sweep sesioni")
    if _immediate_rebalance(setup, ctx):
        total += 1
        breakdown.append("S-6 rebalance")
    spread = ctx.spread
    if spread is not None and spread <= 1.5 * ctx.median_spread():
        total += 1
        breakdown.append("S-7 spread i qetë")

    er = efficiency_ratio(candles[: k + 1])
    if er is not None and er < 0.25:
        total -= 1
        breakdown.append("P-1 chop")
    if ctx.profile.name == "BALANCED" and setup.pda_type in ("FVG", "LIQUIDITY_VOID", "BPR"):
        if any(beyond(c.c, setup.pda_ce, setup.sign) for c in candles[state.tap_index : k + 1]):
            total -= 1
            breakdown.append("P-2 heavy")
    if pda_unverified:
        total -= 1
        breakdown.append("P-3 PDA e paverifikuar")
    if holiday_today:
        total -= 1
        breakdown.append("P-4 pas festës")
    return min(total, 7), breakdown


def _session_sweep(setup: SetupInput, ctx: MarketContext, k_ts: float) -> bool:
    from app.market import ny_day_start

    day_start = ny_day_start(ctx.now_ts)
    candles = [c for c in ctx.candles("M5") if day_start <= c.t <= k_ts]
    if not candles:
        return False
    if setup.is_long:
        low = min(c.l for c in candles)
        asian = ctx.level("asian_low")
        london = ctx.level("london_low")
        return (asian is not None and low < asian) or (london is not None and low < london)
    high = max(c.h for c in candles)
    asian = ctx.level("asian_high")
    london = ctx.level("london_high")
    return (asian is not None and high > asian) or (london is not None and high > london)


def _immediate_rebalance(setup: SetupInput, ctx: MarketContext) -> bool:
    """S-6: at least two same-direction FVGs from different timeframes overlap the entry zone."""
    direction = "BULL" if setup.is_long else "BEAR"
    hits = 0
    for tf in ("M1", "M5", "M15", "H1"):
        candles = ctx.candles(tf)
        if len(candles) < 3:
            continue
        for f in find_fvgs(candles):
            if f.direction != direction:
                continue
            if f.high >= setup.entry_low and f.low <= setup.entry_high:
                hits += 1
                break
    return hits >= 2


def collect_entry_fvgs(setup: SetupInput, ctx: MarketContext) -> list[FVG]:
    """LP-02: same-direction ltf FVGs between stop_loss and the entry zone."""
    direction = "BULL" if setup.is_long else "BEAR"
    low = min(setup.stop_loss, setup.entry_high)
    high = max(setup.stop_loss, setup.entry_high)
    return [
        f
        for f in find_fvgs(ctx.candles(setup.ltf))
        if f.direction == direction and f.low >= low - abs(high - low) and f.high <= high + abs(high - low)
    ]


__all__ = [
    "IntakeResult",
    "RuleResult",
    "SHADOW_MIN_RULES",
    "TriggerState",
    "beyond",
    "chase_limit",
    "collect_entry_fvgs",
    "compute_expiry",
    "compute_score",
    "evaluate_intake",
    "find_cisd",
    "global_block",
    "hard_level",
    "l01_sl_touch",
    "l02_invalidation_close",
    "l03_body_respect",
    "l04_expiry",
    "liquidity_taken",
    "t02_cisd",
    "t03_displacement",
    "t04_freshness",
    "t05_time",
    "t06_spread",
    "t07_price",
    "t08_data",
    "verify_pda",
    "waits_for_text",
]
