"""A fake cTrader Remote MCP server built on the real SDK — including the trading tools.

Every adapter test runs against it; afterwards the recorded trading calls must be empty.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from typing import Any

from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

SYMBOLS = [
    {"symbolId": 41, "symbolName": "XAUUSD", "enabled": True, "description": "Gold", "pipDigits": 3},
    {"symbolId": 101, "symbolName": "BTCUSD", "enabled": True, "description": "Bitcoin", "pipDigits": 2},
    {"symbolId": 1, "symbolName": "EURUSD", "enabled": True, "description": "Euro", "pipDigits": 5},
]


@dataclass
class FakeCTrader:
    """Records every call so a test can assert the trading tools were never reached."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    trading_calls: list[str] = field(default_factory=list)
    fail_next: Exception | None = None
    bid_raw: int = 5654520  # pipettes, 3 digits ⇒ 5654.52
    ask_raw: int = 5654770
    bar_base: int = 5650000
    bar_start_ms: int = 1789556400000  # 2026-09-16 07:00 NY
    unknown_id_empties_batch: bool = True
    rate_limit_hits: int = 0
    include_trading: bool = True
    spot_param: str = "symbolIds"  # some rest-proxy builds call it `symbolId` and still take a list

    def build(self) -> MCPServer:
        server = MCPServer(name="fake-ctrader", version="1.0.18")

        @server.tool()
        def get_version() -> str:
            self.calls.append(("get_version", {}))
            self._maybe_fail()
            return json.dumps({"version": "rest-proxy 1.0.18"})

        @server.tool()
        def get_symbols() -> str:
            self.calls.append(("get_symbols", {}))
            self._maybe_fail()
            return json.dumps({"symbols": SYMBOLS})

        def spot_prices(ids: list[int]) -> str:
            self.calls.append(("get_spot_prices", {self.spot_param: ids}))
            self._maybe_fail()
            known = {s["symbolId"] for s in SYMBOLS}
            if self.unknown_id_empties_batch and any(i not in known for i in ids):
                return json.dumps({"prices": []})  # batch poisoning, exactly as documented
            prices = [
                {"symbolId": i, "bid": self.bid_raw, "ask": self.ask_raw, "timestamp": self.bar_start_ms}
                for i in ids
            ]
            return json.dumps({"prices": prices})

        if self.spot_param == "symbolId":

            @server.tool(name="get_spot_prices")
            def get_spot_prices_singular(symbolId: list[int]) -> str:  # noqa: N803 - mirrors the real API
                return spot_prices(symbolId)

        else:

            @server.tool(name="get_spot_prices")
            def get_spot_prices(symbolIds: list[int]) -> str:  # noqa: N803 - mirrors the real API
                return spot_prices(symbolIds)

        @server.tool()
        def get_trendbars(
            symbolId: int,  # noqa: N803 - mirrors the real API
            period: str,
            fromTimestamp: int,  # noqa: N803
            toTimestamp: int,  # noqa: N803
        ) -> str:
            self.calls.append(
                ("get_trendbars", {"symbolId": symbolId, "period": period, "from": fromTimestamp, "to": toTimestamp})
            )
            self._maybe_fail()
            if period not in ("M_1", "M_5", "M_15", "M_30", "H_1", "H_4", "D_1", "W_1", "MN_1"):
                raise ValueError("-32602 invalid period")
            if toTimestamp - fromTimestamp > 720 * 3600 * 1000:
                raise ValueError("window too wide")
            seconds = {"M_1": 60, "M_5": 300, "M_15": 900, "M_30": 1800, "H_1": 3600, "H_4": 14400, "D_1": 86400}[
                period
            ]
            bars = []
            stamp = fromTimestamp
            index = 0
            while stamp < toTimestamp and index < 600:
                bars.append(
                    {
                        "timestamp": stamp,
                        "open": self.bar_base + index * 100,
                        "high": self.bar_base + index * 100 + 500,
                        "low": self.bar_base + index * 100 - 500,
                        "close": self.bar_base + index * 100 + 200,
                    }
                )
                stamp += seconds * 1000
                index += 1
            return json.dumps({"trendbars": bars})

        if self.include_trading:
            self._register_trading(server)
        return server

    def _register_trading(self, server: MCPServer) -> None:
        """The trading profile's tools. Reaching any of them is a test failure."""
        def make_handler(tool_name: str):
            def handler() -> str:
                self.trading_calls.append(tool_name)
                return json.dumps({"ok": True})

            return handler

        for name in ("create_order", "amend_order", "cancel_order", "amend_position", "close_position"):
            server.add_tool(make_handler(name), name=name, description=f"{name} (must never be called)")

    def _maybe_fail(self) -> None:
        """Remote failures arrive as error results carrying the server's text, not as crashes."""
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise ToolError(str(error))

    def connector(self):
        server = self.build()

        def connect(url: str, token: str):
            self.calls.append(("__connect__", {"url": url, "token": token[:4]}))

            @contextlib.asynccontextmanager
            async def run():
                async with Client(server) as client:
                    yield client

            return run()

        return connect
