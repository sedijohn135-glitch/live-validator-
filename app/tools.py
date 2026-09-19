"""The six MCP tools Gemini Spark calls.

Every input schema is flat (primitives only, no nulls, no nesting) because Gemini's function calling
accepts only a subset of JSON Schema; results are one compact JSON text block (mcp-oauth §5).
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from app.evidence import LATE_ADVANCE_R, PRIMARY, REQUIRED, SCORE_MIN, WEIGHTS
from app.runtime import Runtime

REQUIRED_FIELDS = ("symbol", "stop_loss", "entry (or entry_low + entry_high)")

SYMBOL_ENUM = Literal["XAUUSD", "BTCUSD"]
TIMEFRAME_ENUM = Literal["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"]

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# Gemini asks the owner to tap "Allow" for any tool it sees as a write, which turns every analysis
# into a two-step conversation. The owner asked for the flow to run end to end without that tap, so
# `setup_submit` and `setup_cancel` are advertised as reads too (deviation D14 in docs/SPEC.md).
# Neither tool moves money: they arm or stop the monitoring of one setup on the owner's own service.
SUBMIT_HINTS = ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True)
CANCEL_HINTS = ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True)


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
        return dumps(
            {
                "schema": "rules/2",
                "principle": (
                    "The validator never rejects a setup. It judges only the live evidence at the "
                    "entry touch, then protects the profit."
                ),
                "entry": {
                    "score_min": SCORE_MIN,
                    "primary_required": True,
                    "required": REQUIRED,
                    "required_means": (
                        "the nearest opposing demand (short) or supply (long) must break on M1; "
                        "without it there is no entry, whatever else confirmed"
                    ),
                    "signals": WEIGHTS,
                    "primary_signals": list(PRIMARY),
                    "substitution": (
                        "any signal may stand in for any other: the market rarely gives the exact "
                        "sign the analysis expected, so the engine counts what actually appeared"
                    ),
                    "late_advance_r": LATE_ADVANCE_R,
                },
                "holds_never_reject": ["SPREAD", "KNIFE", "DATA", "FRESH", "REACTION", "STRUCTURE"],
                "cancellations": [
                    "stop touched before the entry was touched",
                    "TP1 touched before the entry was touched",
                ],
                "recalculation": {
                    "stop": "confirmation extreme -/+ max(1.5*ATR(M1), 2*median spread, 2 ticks)",
                    "stop_bounds": "never wider than the submitted stop, never tighter than 0.35R of it",
                    "targets": "the submitted targets are kept; R multiples are recomputed",
                    "secure_level": "nearest swing, session level or round number between entry and TP1, capped at 1R",
                },
                "notes": [
                    "any strategy, any prompt: send entry (or zone), stop and targets",
                    "time, session, kill zone, bias and premium/discount are not the validator's business",
                ],
            }
        )

    @server.tool(
        name="setup_submit",
        description=(
            "Register any setup for live validation, whatever method produced it. Required: symbol, "
            "stop_loss and either entry or entry_low+entry_high. Everything else is optional and "
            "nothing is ever rejected: the validator watches the zone and answers ENTER NOW or a "
            "recalculated LIMIT on the owner's Telegram."
        ),
        annotations=SUBMIT_HINTS,
        structured_output=False,
    )
    async def setup_submit(
        symbol: Annotated[SYMBOL_ENUM, Field(description="Instrument of the setup")],
        stop_loss: Annotated[float, Field(description="Stop loss price")],
        entry: Annotated[float, Field(description="Single entry price, or 0 when a zone is given")] = 0.0,
        entry_low: Annotated[float, Field(description="Lower edge of the entry zone")] = 0.0,
        entry_high: Annotated[float, Field(description="Upper edge of the entry zone")] = 0.0,
        # A plain string, never an enum: an enum carrying "" as a member is rejected by Gemini's
        # function calling, and a rejected declaration fails the whole app, not just this tool.
        direction: Annotated[
            str, Field(description="Optional: LONG or SHORT. Leave empty to infer it from the stop")
        ] = "",
        tp1: Annotated[float, Field(description="First target, omit for 1R/2R/3R defaults")] = 0.0,
        tp2: Annotated[float, Field(description="Second target, omit if unused")] = 0.0,
        tp3: Annotated[float, Field(description="Third target, omit if unused")] = 0.0,
        label: Annotated[str, Field(description="Free name of the model or strategy, for your own reference")] = "",
        note: Annotated[str, Field(description="Short reasoning, max 300 characters")] = "",
        client_ref: Annotated[str, Field(description="Free reference you can use to find this setup again")] = "",
    ) -> str:
        payload = {
            "symbol": symbol,
            "stop_loss": stop_loss,
            "entry": entry,
            "entry_low": entry_low,
            "entry_high": entry_high,
            "direction": direction,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "label": label,
            "note": note,
            "client_ref": client_ref,
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
