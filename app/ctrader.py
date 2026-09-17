"""cTrader Remote MCP adapter (IC Markets): read-only allowlist, decoding, rate limits, hot swap.

The adapter is physically unable to trade: `call()` refuses anything outside `ALLOWED_TOOLS` and
there is no override flag and no environment switch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import TIMEFRAMES, Settings
from app.market import Candle

logger = logging.getLogger(__name__)

ALLOWED_TOOLS = frozenset({"get_version", "get_symbols", "get_spot_prices", "get_trendbars"})

# Detection only. These names exist in exactly one place in `app/` and are never called.
KNOWN_TRADING_TOOLS = frozenset(
    {"create_order", "amend_order", "cancel_order", "amend_position", "close_position"}
)

HISTORICAL_TOOLS = frozenset({"get_trendbars"})

# The live `tools/list` schema is the authority on argument names (ctrader reference §5): builds
# differ, e.g. `symbolId` carrying an array where an older doc said `symbolIds`. For each logical
# argument we try these names in order and keep the first the server actually declares.
ARG_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "get_spot_prices": {
        "symbol_ids": ("symbolIds", "symbolId", "symbol_ids", "symbol_id", "ids", "symbols"),
    },
    "get_trendbars": {
        "symbol_id": ("symbolId", "symbol_id", "symbolIds", "id"),
        "period": ("period", "timeframe", "granularity"),
        "from": ("fromTimestamp", "from_timestamp", "from", "fromDate", "startTimestamp"),
        "to": ("toTimestamp", "to_timestamp", "to", "toDate", "endTimestamp"),
    },
}

TIME_ARGS = frozenset({"from", "to"})

# A window bound may be epoch milliseconds, the same number as a string, or ISO-8601 Z, depending on
# the build. The schema decides; when it only says "string", we try ISO first and fall back once.
TIME_FORMATS = ("iso", "epoch_string")

PERIODS = {
    "M1": "M_1",
    "M5": "M_5",
    "M15": "M_15",
    "M30": "M_30",
    "H1": "H_1",
    "H4": "H_4",
    "D1": "D_1",
    "W1": "W_1",
}

MAX_WINDOW_S = 720 * 3600  # the server refuses wider request windows
MAX_BARS_PER_CALL = 90  # the proxy truncates a response at ~100 bars whatever the window
PRECISION_TABLE = {"XAUUSD": 3, "BTCUSD": 2}

AUTH_PATTERN = re.compile(r"unauthori|forbidden|expired|invalid token|session", re.IGNORECASE)
URL_KEYS = ("url", "serverurl", "httpurl", "endpoint", "server_url", "http_url", "uri")
TOKEN_KEYS = ("authorization", "token", "bearer", "access_token", "accesstoken", "apikey", "api_key")


class ForbiddenTool(Exception):
    """Raised when anything outside the read-only allowlist is requested."""


class AuthError(Exception):
    """Credentials expired or rejected: pause and ask the owner for a new configuration."""


class DataError(Exception):
    """Any other cTrader failure: outage logic applies."""


def mask(token: str) -> str:
    if not token:
        return "-"
    if len(token) <= 10:
        return "…"
    return f"{token[:4]}…{token[-4:]}"


@dataclass(frozen=True)
class Credentials:
    url: str
    token: str
    source: str

    @property
    def masked(self) -> str:
        return mask(self.token)


def _find_in(data: Any, keys: tuple[str, ...]) -> str | None:
    """Depth-first search for the first string value under any of `keys` (case-insensitive)."""
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower().replace("-", "_") in keys and isinstance(value, str) and value.strip():
                return value.strip()
        for value in data.values():
            found = _find_in(value, keys)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_in(item, keys)
            if found:
                return found
    return None


def _clean_token(raw: str) -> str:
    token = raw.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def parse_config(raw: str, known_url: str = "") -> Credentials | None:
    """Parse a pasted cTrader Remote MCP configuration, a JSON blob of any shape, or a bare token."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if data is not None:
        url = _find_in(data, URL_KEYS) or known_url
        token = _find_in(data, TOKEN_KEYS)
        if token:
            token = _clean_token(token)
        if url and token:
            return Credentials(url.rstrip("/"), token, "config")
        return None
    url_match = re.search(r"https?://[^\s\"',;}\]]+", text)
    url = url_match.group(0).rstrip("/") if url_match else known_url
    token_match = re.search(r"(?i)bearer\s+([A-Za-z0-9._\-]+)", text)
    if token_match:
        token = token_match.group(1)
    else:
        candidates = [w for w in re.split(r"[\s,;'\"]+", text) if len(w) >= 16 and not w.startswith("http")]
        token = _clean_token(candidates[-1]) if candidates else ""
    if url and token:
        return Credentials(url.rstrip("/"), token, "token")
    return None


def resolve_credentials(settings: Settings, override: str | None) -> Credentials | None:
    """kv override (Telegram `/ctrader`) → CTRADER_MCP_CONFIG → CTRADER_MCP_URL + CTRADER_MCP_TOKEN."""
    known_url = settings.ctrader_url
    if override:
        parsed = parse_config(override, known_url)
        if parsed:
            return Credentials(parsed.url, parsed.token, "telegram")
    if settings.ctrader_config:
        parsed = parse_config(settings.ctrader_config, known_url)
        if parsed:
            return Credentials(parsed.url, parsed.token, "env_config")
    if settings.ctrader_url and settings.ctrader_token:
        return Credentials(settings.ctrader_url.rstrip("/"), _clean_token(settings.ctrader_token), "env_pair")
    return None


def classify(error: BaseException) -> Exception:
    """Auth vs data, reading the whole exception chain: cTrader errors may be plain strings."""
    texts: list[str] = []
    status: int | None = None
    current: BaseException | None = error
    seen = 0
    while current is not None and seen < 8:
        texts.append(str(current))
        response = getattr(current, "response", None)
        status = status or getattr(response, "status_code", None)
        current = current.__cause__ or current.__context__
        seen += 1
    joined = " | ".join(texts)
    if status in (401, 403) or AUTH_PATTERN.search(joined):
        return AuthError(texts[0] or joined)
    return DataError(texts[0] or joined)


class RateLimiter:
    """Token buckets for the documented limits, plus a concurrency cap."""

    def __init__(self, general_per_s: float = 20.0, historical_per_s: float = 4.0, concurrency: int = 2) -> None:
        self.general = general_per_s
        self.historical = historical_per_s
        self._general_at = float("-inf")
        self._historical_at = float("-inf")
        self._semaphore = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()

    async def acquire(self, historical: bool, sleep=asyncio.sleep, clock=time.monotonic) -> None:
        async with self._lock:
            now = clock()
            gap = 1.0 / (self.historical if historical else self.general)
            last = self._historical_at if historical else self._general_at
            wait = max(0.0, last + gap - now)
            if historical:
                self._historical_at = now + wait
            else:
                self._general_at = now + wait
        if wait > 0:
            await sleep(wait)

    def slot(self):
        return self._semaphore


@dataclass
class SymbolInfo:
    name: str
    symbol_id: int
    digits: int
    calibrated: bool = False
    enabled: bool = True


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    ts: float

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass
class CTraderClient:
    """One long-lived MCP session with reconnect, allowlist and price decoding."""

    settings: Settings
    store: Any
    connector: Callable[[str, str], Any] | None = None
    limiter: RateLimiter = field(default_factory=RateLimiter)
    clock: Callable[[], float] = time.time

    credentials: Credentials | None = None
    tool_names: tuple[str, ...] = ()
    tool_schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    time_format: str = "iso"
    symbols: dict[str, SymbolInfo] = field(default_factory=dict)
    version: str = ""
    profile_kind: str = "unknown"
    status: str = "not_configured"
    last_error: str = ""
    warnings: list[str] = field(default_factory=list)

    _client: Any = None
    _stack: contextlib.AsyncExitStack | None = None
    _backoff: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)

    # ------------------------------------------------------------- connection
    def load_credentials(self) -> Credentials | None:
        override = self.store.get_kv("ctrader_override") if self.store else None
        self.credentials = resolve_credentials(self.settings, override)
        if self.credentials is None:
            self.status = "not_configured"
        return self.credentials

    async def connect(self) -> None:
        if self._client is not None:
            return
        credentials = self.credentials or self.load_credentials()
        if credentials is None:
            raise DataError("cTrader nuk është konfiguruar")
        factory = self.connector or _default_connector
        stack = contextlib.AsyncExitStack()
        try:
            self._client = await stack.enter_async_context(factory(credentials.url, credentials.token))
        except BaseException as exc:
            await stack.aclose()
            raise classify(exc) from exc
        self._stack = stack

    async def aclose(self) -> None:
        stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()

    async def reconnect(self, sleep=asyncio.sleep) -> None:
        await self.aclose()
        for delay in self._backoff:
            try:
                await self.connect()
                return
            except AuthError:
                raise
            except Exception as exc:  # pragma: no cover - exercised through call()
                self.last_error = str(exc)
                await sleep(delay)
        raise DataError(self.last_error or "s'u lidh me cTrader")

    async def swap_credentials(self, raw: str | None) -> Credentials | None:
        """Hot swap without a redeploy: store, reconnect, rediscover."""
        if raw is None:
            self.store.delete_kv("ctrader_override")
        else:
            self.store.set_kv("ctrader_override", raw)
        await self.aclose()
        self.load_credentials()
        if self.credentials is None:
            return None
        await self.discover()
        return self.credentials

    # ------------------------------------------------------------------ calls
    async def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        """The only way out to cTrader. Anything outside the allowlist is refused here."""
        if tool not in ALLOWED_TOOLS:
            raise ForbiddenTool(f"tool '{tool}' is not read-only and can never be called")
        await self.connect()
        await self.limiter.acquire(tool in HISTORICAL_TOOLS)
        async with self.limiter.slot():
            try:
                result = await self._client.call_tool(tool, args or {})
            except BaseException as exc:
                error = classify(exc)
                if isinstance(error, AuthError):
                    self.status = "auth_error"
                    self.last_error = str(error)
                    raise error from exc
                self.status = "down"
                self.last_error = str(error)
                raise error from exc
        if getattr(result, "is_error", False):
            error = classify(RuntimeError(_error_text(result)))
            self.status = "auth_error" if isinstance(error, AuthError) else "down"
            self.last_error = str(error)
            raise error
        payload = _decode_result(result)
        if isinstance(payload, dict) and payload.get("error"):
            raise classify(RuntimeError(str(payload["error"])))
        self.status = "ok"
        return payload

    # -------------------------------------------------------------- discovery
    async def discover(self) -> dict[str, Any]:
        """`get_version`, `tools/list` and the symbol map. Never calls a trading tool."""
        await self.connect()
        listed = await self._client.list_tools()
        tools = getattr(listed, "tools", listed)
        names = tuple(sorted(getattr(t, "name", str(t)) for t in tools))
        self.tool_names = names
        self.tool_schemas = {
            getattr(t, "name", ""): (getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {})
            for t in tools
            if getattr(t, "name", "") in ALLOWED_TOOLS
        }
        self.profile_kind = "trading" if KNOWN_TRADING_TOOLS & set(names) else "data"
        if self.store:
            self.store.set_json("ctrader_tools", list(names))
        self._pick_time_format()
        version = await self.call("get_version")
        self.version = str(_first_value(version, ("version", "build", "rest_proxy")) or version)
        await self.load_symbols()
        return {"tools": names, "profile": self.profile_kind, "version": self.version}

    async def load_symbols(self) -> dict[str, SymbolInfo]:
        payload = await self.call("get_symbols")
        entries = _as_list(payload, "symbols")
        resolved: dict[str, SymbolInfo] = {}
        for wanted in self.settings.symbols:
            info = self._resolve_symbol(wanted, entries)
            if info is None:
                self.warnings.append(f"simboli {wanted} nuk u gjet te cTrader")
                continue
            resolved[wanted] = info
        self.symbols = resolved
        if self.store:
            self.store.set_json(
                "ctrader_symbols", {k: {"id": v.symbol_id, "digits": v.digits} for k, v in resolved.items()}
            )
        return resolved

    def _resolve_symbol(self, wanted: str, entries: list[dict[str, Any]]) -> SymbolInfo | None:
        mapped = self.settings.symbol_map.get(wanted)
        names = {str(e.get("symbolName") or e.get("name") or ""): e for e in entries}
        candidate = None
        if mapped and mapped in names:
            candidate = names[mapped]
        elif wanted in names:
            candidate = names[wanted]
        else:
            lowered = {k.lower(): v for k, v in names.items()}
            if wanted.lower() in lowered:
                candidate = lowered[wanted.lower()]
            else:
                starts = [v for k, v in names.items() if k.upper().startswith(wanted.upper())]
                if len(starts) == 1:
                    candidate = starts[0]
                elif len(starts) > 1:
                    self.warnings.append(f"simboli {wanted} është i paqartë te cTrader")
                    return None
        if candidate is None:
            close = [n for n in names if n and wanted[:3].upper() in n.upper()][:5]
            hint = f" — te cTrader gjenden: {', '.join(close)}" if close else ""
            self.warnings.append(f"simboli {wanted} nuk u gjet{hint}")
            return None
        symbol_id = candidate.get("symbolId") or candidate.get("id")
        if symbol_id is None:
            return None
        return SymbolInfo(wanted, int(symbol_id), self._digits_for(wanted, candidate))

    def _digits_for(self, symbol: str, entry: dict[str, Any]) -> int:
        for key in ("pipDigits", "digits", "pipPosition", "decimals"):
            value = entry.get(key)
            if isinstance(value, int) and 0 <= value <= 10:
                return value
        override = self.settings.symbol(symbol).display_decimals
        table = PRECISION_TABLE.get(symbol.upper())
        return table if table is not None else override

    # ------------------------------------------------------------- arguments
    def _properties(self, tool: str) -> dict[str, Any]:
        schema = self.tool_schemas.get(tool) or {}
        properties = schema.get("properties")
        return properties if isinstance(properties, dict) else {}

    def build_args(self, tool: str, values: dict[str, Any]) -> dict[str, Any]:
        """Map our logical argument names onto whatever this server's schema actually declares."""
        properties = self._properties(tool)
        aliases = ARG_ALIASES.get(tool, {})
        out: dict[str, Any] = {}
        for logical, value in values.items():
            candidates = aliases.get(logical, (logical,))
            name = next((c for c in candidates if c in properties), candidates[0])
            spec = properties.get(name)
            out[name] = (
                _fit_time(spec, value, self.time_format) if logical in TIME_ARGS else _fit_type(spec, value)
            )
        return out

    def _pick_time_format(self) -> None:
        """Read the window-bound type from the schema; ISO unless it names epoch milliseconds."""
        properties = self._properties("get_trendbars")
        spec = properties.get("fromTimestamp") or properties.get("from") or {}
        if not isinstance(spec, dict) or spec.get("type") != "string":
            return
        text = f"{spec.get('format', '')} {spec.get('description', '')}".lower()
        self.time_format = "epoch_string" if ("epoch" in text or "milli" in text) else "iso"

    # --------------------------------------------------------------- decoding
    def decode(self, symbol: str, raw: float) -> float | None:
        """Pipettes → display price, with band calibration (ctrader reference §6)."""
        info = self.symbols.get(symbol)
        band = self.settings.symbol(symbol).price_band
        if raw != int(raw):  # already a display price
            return raw if band[0] <= raw <= band[1] else None
        digits = info.digits if info else PRECISION_TABLE.get(symbol.upper(), 2)
        value = raw / (10**digits)
        if band[0] <= value <= band[1]:
            return value
        matches = [raw / (10**e) for e in range(0, 9) if band[0] <= raw / (10**e) <= band[1]]
        if len(matches) == 1:
            new_digits = next(e for e in range(0, 9) if band[0] <= raw / (10**e) <= band[1])
            if info is not None and not info.calibrated:
                self.symbols[symbol] = SymbolInfo(info.name, info.symbol_id, new_digits, calibrated=True)
                self.warnings.append(f"{symbol}: shkalla e çmimit u kalibrua automatikisht ({new_digits} shifra)")
            return matches[0]
        self.warnings.append(f"{symbol}: çmimi s'u dekodua — vendos PRICE_DIGITS")
        if info is not None:
            self.symbols[symbol] = SymbolInfo(info.name, info.symbol_id, info.digits, info.calibrated, enabled=False)
        return None

    # ----------------------------------------------------------------- quotes
    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """One batched call. Unknown ids are dropped first: a bad id empties the whole batch."""
        ids: list[int] = []
        by_id: dict[int, str] = {}
        for name in symbols:
            info = self.symbols.get(name)
            if info is None or not info.enabled:
                continue
            ids.append(info.symbol_id)
            by_id[info.symbol_id] = name
        if not ids:
            raise DataError("asnjë simbol i kërkuar nuk u zgjidh te cTrader")
        payload = await self.call("get_spot_prices", self.build_args("get_spot_prices", {"symbol_ids": ids}))
        out: dict[str, Quote] = {}
        for entry in _as_list(payload, "prices"):
            symbol_id = entry.get("symbolId") or entry.get("id")
            if symbol_id is None or int(symbol_id) not in by_id:
                continue
            name = by_id[int(symbol_id)]
            bid = self.decode(name, float(entry.get("bid", 0) or 0))
            ask = self.decode(name, float(entry.get("ask", 0) or 0))
            if bid is None or ask is None:
                continue
            stamp = entry.get("timestamp") or entry.get("ts")
            out[name] = Quote(name, bid, ask, _to_seconds(stamp) if stamp else self.clock())
        return out

    # ---------------------------------------------------------------- candles
    async def candles(self, symbol: str, timeframe: str, count: int = 100, end_ts: float | None = None):
        """Closed candles, oldest first, chunked so no request window exceeds 720 hours."""
        info = self.symbols.get(symbol)
        if info is None:
            raise DataError(f"simboli {symbol} nuk u zgjidh te cTrader")
        if not info.enabled:
            raise DataError(f"simboli {symbol} është çaktivizuar (dekodimi i çmimit dështoi)")
        period = PERIODS.get(timeframe.upper())
        if period is None:
            raise DataError(f"timeframe {timeframe} is not supported by cTrader")
        tf = TIMEFRAMES[timeframe.upper()]
        end = self.clock() if end_ts is None else end_ts
        start = end - count * tf
        collected: dict[float, Candle] = {}
        cursor = start
        # Two caps apply: the documented 720-hour window and an undocumented per-response bar limit.
        # Asking for a wide window silently returns only its newest bars, which left the Asian range
        # and the liquidity lookback empty, so every chunk stays under the bar limit too.
        span = min(MAX_WINDOW_S, MAX_BARS_PER_CALL * tf)
        guard = 0
        while cursor < end and guard < 60:
            guard += 1
            chunk_end = min(end, cursor + span)
            window = {
                "symbol_id": info.symbol_id,
                "period": period,
                "from": int(cursor * 1000),
                "to": int(chunk_end * 1000),
            }
            try:
                payload = await self.call("get_trendbars", self.build_args("get_trendbars", window))
            except DataError as exc:
                if not self._flip_time_format(exc):
                    raise
                payload = await self.call("get_trendbars", self.build_args("get_trendbars", window))
            for bar in _as_list(payload, "trendbars", "bars"):
                candle = self._decode_bar(symbol, bar, tf, end)
                if candle is not None:
                    collected[candle.t] = candle
            cursor = chunk_end
        return [collected[t] for t in sorted(collected)][-count:]

    def _decode_bar(self, symbol: str, bar: dict[str, Any], tf: int, now: float) -> Candle | None:
        stamp = bar.get("timestamp") or bar.get("utcTimestampInMinutes") or bar.get("t")
        if stamp is None:
            return None
        open_ts = _to_seconds(stamp)
        if open_ts + tf > now:  # never hand a forming bar to the engine
            return None
        values = []
        for key in ("open", "high", "low", "close"):
            raw = bar.get(key, bar.get(key[0]))
            if raw is None:
                return None
            decoded = self.decode(symbol, float(raw))
            if decoded is None:
                return None
            values.append(decoded)
        return Candle(open_ts, *values)

    def _flip_time_format(self, error: Exception) -> bool:
        """True when the failure looks like a rejected window bound and another format is left to try."""
        text = str(error).lower()
        if "-32602" not in text and "invalid" not in text:
            return False
        if not any(word in text for word in ("timestamp", "from", "to", "date")):
            return False
        remaining = [f for f in TIME_FORMATS if f != self.time_format]
        if not remaining:
            return False
        self.time_format = remaining[0]
        self.warnings.append(f"formati i kohës u ndërrua në {self.time_format}")
        logger.info("retrying get_trendbars with time_format=%s", self.time_format)
        return True

    def cross_check(self, symbol: str, bid: float, last_close: float | None) -> bool:
        """Decoding sanity: the newest M1 close must be within 1 % of the live bid."""
        if last_close is None or bid <= 0:
            return True
        if abs(last_close - bid) / bid <= 0.01:
            return True
        self.warnings.append(f"{symbol}: çmimi i qirinjve s'përputhet me bid-in — simboli u çaktivizua")
        info = self.symbols.get(symbol)
        if info is not None:
            self.symbols[symbol] = SymbolInfo(info.name, info.symbol_id, info.digits, info.calibrated, enabled=False)
        return False


# ----------------------------------------------------------------- utilities
def _default_connector(url: str, token: str):
    """The real transport: a streamable-HTTP MCP client carrying the bearer token."""

    @contextlib.asynccontextmanager
    async def run():
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        timeout = httpx2.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=timeout) as http:
            async with Client(streamable_http_client(url, http_client=http)) as client:
                yield client

    return run()


def _fit_time(spec: Any, epoch_ms: int, mode: str) -> Any:
    declared = (spec or {}).get("type") if isinstance(spec, dict) else None
    if declared != "string":
        return epoch_ms
    if mode == "epoch_string":
        return str(int(epoch_ms))
    return datetime.fromtimestamp(int(epoch_ms) / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fit_type(spec: Any, value: Any) -> Any:
    """Wrap or unwrap a value so it matches the array-ness the schema declares."""
    declared = (spec or {}).get("type") if isinstance(spec, dict) else None
    if declared == "array" and not isinstance(value, list):
        return [value]
    if declared in ("integer", "number", "string") and isinstance(value, list):
        return value[0] if len(value) == 1 else value
    return value


def _error_text(result: Any) -> str:
    content = getattr(result, "content", None) or []
    texts = [c.text for c in content if getattr(c, "type", "") == "text"]
    return " ".join(texts) or "cTrader error"


def _decode_result(result: Any) -> Any:
    content = getattr(result, "content", None)
    if content:
        texts = [c.text for c in content if getattr(c, "type", "") == "text"]
        if texts:
            joined = "\n".join(texts)
            try:
                return json.loads(joined)
            except ValueError:
                return joined
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    return result


def _as_list(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        for value in payload.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
    return []


def _first_value(payload: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(payload, dict):
        for key in keys:
            if key in payload:
                return payload[key]
    return None


def _to_seconds(stamp: Any) -> float:
    value = float(stamp)
    return value / 1000.0 if value > 1e11 else value
