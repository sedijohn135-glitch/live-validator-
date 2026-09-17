"""The six MCP tools Gemini Spark calls.

Every input schema is flat (primitives only, no nulls, no nesting) because Gemini's function calling
accepts only a subset of JSON Schema; results are one compact JSON text block (mcp-oauth §5).
"""

from __future__ import annotations

import json
from dataclasses import MISSING, fields
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from app.config import ENTRY_MODELS, PDA_TYPES
from app.runtime import Runtime
from app.setup_model import SetupInput

REQUIRED_FIELDS = tuple(
    f.name
    for f in fields(SetupInput)
    if f.default is MISSING and f.default_factory is MISSING and f.name != "warnings"
)

SYMBOL_ENUM = Literal["XAUUSD", "BTCUSD"]
TIMEFRAME_ENUM = Literal["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"]

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
SUBMIT_HINTS = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)
CANCEL_HINTS = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True)


def dumps(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)


def _optional(value: float, sentinel: float = 0.0) -> float | None:
    """Gemini cannot send nulls, so "absent" is encoded as the sentinel default."""
    return None if value == sentinel else value


def _optional_text(value: str) -> str | None:
    return value.strip() or None


def register(server: MCPServer, runtime: Runtime) -> None:
    """Register every tool on `server`. Descriptions tell Gemini how to behave."""

    @server.tool(
        name="market_snapshot",
        description=(
            "Live IC Markets cTrader data for one symbol: New York time context, bid/ask, session "
            "levels, ATR, swings, fair value gaps and recent candles. Use ONLY these prices and "
            "times; never prices from memory. Copy an FVG's low/high/formed_at verbatim into "
            "setup_submit."
        ),
        annotations=READ_ONLY,
        structured_output=False,
    )
    async def market_snapshot(
        symbol: Annotated[SYMBOL_ENUM, Field(description="Instrument to snapshot")],
    ) -> str:
        return dumps(await runtime.snapshot(symbol))

    @server.tool(
        name="market_candles",
        description=(
            "Closed candles for one symbol and timeframe, oldest first, as [New York time, open, "
            "high, low, close]. Use at most three calls per analysis."
        ),
        annotations=READ_ONLY,
        structured_output=False,
    )
    async def market_candles(
        symbol: Annotated[SYMBOL_ENUM, Field(description="Instrument to read")],
        timeframe: Annotated[TIMEFRAME_ENUM, Field(description="Candle timeframe")],
        count: Annotated[int, Field(description="How many closed candles, 1 to 500", ge=1, le=500)] = 100,
    ) -> str:
        await runtime.ensure_history(symbol, (timeframe,))
        await runtime.refresh_candles(symbol, (timeframe,), count=3)
        decimals = runtime.settings.symbol(symbol).display_decimals
        series = runtime.candles.series(symbol, timeframe)[-count:]
        return dumps(
            {
                "schema": "candles/1",
                "symbol": symbol,
                "timeframe": timeframe,
                "count": len(series),
                "candles": [c.as_list(decimals) for c in series],
                "notes": ["times are New York", "candle time = open time", "closed candles only"],
            }
        )

    @server.tool(
        name="validator_rules",
        description=(
            "The active validator profile and its thresholds: what a setup must satisfy to be armed "
            "and what must happen live before an ENTER message is sent."
        ),
        annotations=READ_ONLY,
        structured_output=False,
    )
    async def validator_rules() -> str:
        profile = runtime.settings.profile
        return dumps(
            {
                "schema": "rules/1",
                "profile": profile.name,
                "thresholds": {
                    "rr_min_plan": profile.rr_min_plan,
                    "rr_min_trigger": profile.rr_min_trigger,
                    "checklist_min_positive": profile.checklist_min_pos,
                    "checklist_max_negative": profile.checklist_max_neg,
                    "score_min": profile.score_min,
                    "ce_is_hard_level": profile.ce_hard_fvg,
                    "max_setup_lifetime_h": profile.max_setup_lifetime_h,
                    "chase_max_risk_fraction": profile.chase_max_risk_frac,
                    "confirm_max_bars": profile.confirm_max_bars,
                    "level_tolerance_atr": profile.level_tol_atr,
                    "news_blackout_min": [profile.news_before_min, profile.news_after_min],
                    "friday_cutoff_ny_xauusd": profile.friday_cutoff_ny,
                },
                "entry_models": list(ENTRY_MODELS),
                "pda_types": list(PDA_TYPES),
                "required_fields": list(REQUIRED_FIELDS),
                "notes": [
                    "the validator decides the entry, not the analysis",
                    "a rejected setup must not be resubmitted with looser levels",
                ],
            }
        )

    @server.tool(
        name="setup_submit",
        description=(
            "Submit one ICT v11 setup for live validation. Numbers only, on the same scale as "
            "market_snapshot. LONG: stop_loss < entry_low < entry_high < tp1 < tp2 < tp3; SHORT is "
            "the reverse. Never resubmit a rejected setup with loosened levels."
        ),
        annotations=SUBMIT_HINTS,
        structured_output=False,
    )
    async def setup_submit(
        symbol: Annotated[SYMBOL_ENUM, Field(description="Instrument of the setup")],
        direction: Annotated[Literal["LONG", "SHORT"], Field(description="Trade direction")],
        entry_model: Annotated[str, Field(description="v11 entry model name, e.g. ICT_2022")],
        htf_timeframe: Annotated[Literal["D1", "H4", "H1"], Field(description="Higher timeframe used")],
        htf_bias: Annotated[Literal["BULLISH", "BEARISH"], Field(description="Higher timeframe bias")],
        ltf: Annotated[Literal["M1", "M5", "M15"], Field(description="Timeframe the confirmation is read on")],
        entry_low: Annotated[float, Field(description="Lower edge of the entry zone")],
        entry_high: Annotated[float, Field(description="Upper edge of the entry zone")],
        stop_loss: Annotated[float, Field(description="Stop loss price beyond the structural swing")],
        tp1: Annotated[float, Field(description="First target price")],
        invalidation_level: Annotated[float, Field(description="Price whose close kills the setup")],
        pda_type: Annotated[str, Field(description="PDA array type, e.g. FVG or ORDER_BLOCK")],
        pda_timeframe: Annotated[str, Field(description="Timeframe the PDA was read on")],
        pda_low: Annotated[float, Field(description="Lower edge of the PDA zone")],
        pda_high: Annotated[float, Field(description="Upper edge of the PDA zone")],
        pda_formed_at_ny: Annotated[
            str, Field(description="PDA open time as 'YYYY-MM-DD HH:MM' New York, copied from the snapshot")
        ],
        opposing_liquidity_level: Annotated[float, Field(description="SSL for LONG or BSL for SHORT")],
        opposing_liquidity_taken: Annotated[bool, Field(description="True only if the candles show it was swept")],
        price_at_analysis: Annotated[float, Field(description="Bid price taken from the snapshot")],
        checklist_positive: Annotated[int, Field(description="v11 Step 11 positive count", ge=0, le=30)],
        checklist_negative: Annotated[int, Field(description="v11 Step 11 negative count", ge=0, le=30)],
        confidence: Annotated[float, Field(description="Confidence 0 to 100", ge=0, le=100)],
        rationale: Annotated[str, Field(description="Two short sentences of reasoning, max 600 characters")],
        tp2: Annotated[float, Field(description="Second target price, omit if unused")] = 0.0,
        tp3: Annotated[float, Field(description="Third target price, omit if unused")] = 0.0,
        invalidation_timeframe: Annotated[
            str, Field(description="Timeframe for the invalidation close, defaults to the LTF")
        ] = "",
        pda_mean_threshold: Annotated[float, Field(description="Order block mean threshold, omit if unused")] = 0.0,
        range_high: Annotated[float, Field(description="High of the dealing range for premium/discount")] = 0.0,
        range_low: Annotated[float, Field(description="Low of the dealing range for premium/discount")] = 0.0,
        model_ref_level: Annotated[float, Field(description="Model 2 six AM price or Venom BISI close")] = 0.0,
        kill_zone: Annotated[str, Field(description="Kill zone name from the analysis")] = "",
        macro: Annotated[str, Field(description="Macro window name from the analysis")] = "",
        valid_until_ny: Annotated[str, Field(description="Latest useful time as 'YYYY-MM-DD HH:MM' New York")] = "",
        client_ref: Annotated[str, Field(description="Free reference you can use to find this setup again")] = "",
    ) -> str:
        payload = {
            "symbol": symbol,
            "direction": direction,
            "entry_model": entry_model,
            "htf_timeframe": htf_timeframe,
            "htf_bias": htf_bias,
            "ltf": ltf,
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": stop_loss,
            "tp1": tp1,
            "invalidation_level": invalidation_level,
            "pda_type": pda_type,
            "pda_timeframe": pda_timeframe,
            "pda_low": pda_low,
            "pda_high": pda_high,
            "pda_formed_at_ny": pda_formed_at_ny,
            "opposing_liquidity_level": opposing_liquidity_level,
            "opposing_liquidity_taken": opposing_liquidity_taken,
            "price_at_analysis": price_at_analysis,
            "checklist_positive": checklist_positive,
            "checklist_negative": checklist_negative,
            "confidence": confidence,
            "rationale": rationale,
            "tp2": _optional(tp2),
            "tp3": _optional(tp3),
            "invalidation_timeframe": _optional_text(invalidation_timeframe),
            "pda_mean_threshold": _optional(pda_mean_threshold),
            "range_high": _optional(range_high),
            "range_low": _optional(range_low),
            "model_ref_level": _optional(model_ref_level),
            "kill_zone": _optional_text(kill_zone),
            "macro": _optional_text(macro),
            "valid_until_ny": _optional_text(valid_until_ny),
            "client_ref": _optional_text(client_ref),
        }
        await runtime.prepare_for_submit(symbol)
        return dumps(runtime.engine.submit(payload))

    @server.tool(
        name="setup_status",
        description=(
            "State of one setup by id, or the list of active and recently closed setups when no id "
            "is given. Includes live distance to the zone, the stop and the targets."
        ),
        annotations=READ_ONLY,
        structured_output=False,
    )
    async def setup_status(
        setup_id: Annotated[str, Field(description="Setup id such as XAU-0916-K7QD, omit for the overview")] = "",
    ) -> str:
        return dumps(runtime.engine.status(_optional_text(setup_id)))

    @server.tool(
        name="setup_cancel",
        description=(
            "Cancel a setup that has not been triggered yet. A triggered setup can no longer be "
            "cancelled; it is only tracked."
        ),
        annotations=CANCEL_HINTS,
        structured_output=False,
    )
    async def setup_cancel(
        setup_id: Annotated[str, Field(description="Setup id to cancel, such as XAU-0916-K7QD")],
    ) -> str:
        return dumps(runtime.engine.cancel(setup_id.strip()))

    # Keep the registered callables referenced so linters see them as used.
    _ = (market_snapshot, market_candles, validator_rules, setup_submit, setup_status, setup_cancel)
