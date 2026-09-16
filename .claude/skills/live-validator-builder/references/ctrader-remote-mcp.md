# cTrader Remote MCP adapter (IC Markets)

## 1. Read the official docs first (do NOT copy them into the repo — Spotware EULA, proprietary)

- https://help.ctrader.com/ctrader-ai-agent-connect/remote-mcp/setup/ and …/remote-mcp/analysis/ and …/faq/
- https://github.com/spotware/ctrader-skills → `skills/ctrader-mcp-servers/references/remote-http-server.md`,
  `known-quirks.md`, `self-healing-playbook.md`, `assets/symbol_precision_table.json`
Read them in the build sandbox (curl/git clone to /tmp) and implement from them. The facts below were verified against
those docs (rest-proxy 1.0.18, docs dated May 2026); if the live `tools/list` differs, the live server wins and the
difference goes into `docs/SPEC.md`.

## 2. Verified facts

- Setup: owner logs into cTrader Web with cTID → Settings → **Remote MCP** → copies a configuration containing the server
  URL and a **per-trading-account bearer token**. The server authenticates via the active cTrader Web session: when that
  session expires the token stops working and must be copied again.
- Two profiles. **data**: `get_version, get_balance, get_assets, get_symbols, get_spot_prices, get_trendbars, get_positions,
  get_position_details, get_pending_orders, get_order_history, get_deals`. **trading**: data + `create_order, amend_order,
  cancel_order, amend_position, close_position`. The visible tool list tells which profile the token has.
- Symbols are numeric `symbolId` everywhere; map names via `get_symbols` (fields incl. `symbolId`, `symbolName`, `enabled`,
  `description`), cache per session.
- `get_spot_prices` takes an array of symbolIds (batched). **Batch poisoning**: one unknown id ⇒ EMPTY `prices[]` for the
  whole batch, silently. Validate every id against the cache before calling.
- `get_trendbars(symbolId, period, fromTimestamp, toTimestamp[, count])`; `period` accepts exactly
  `M_1, M_5, M_15, M_30, H_1, H_4, D_1, W_1, MN_1` (others ⇒ -32602). Window ≤ **720 h** per call (chunk), `count` ≤
  server limit, `hasMore` may appear. Unknown symbol ⇒ 502 `UNKNOWN_SYMBOL`.
- Timestamps in responses are **epoch milliseconds**; request windows accept epoch ms or ISO-8601 `Z`.
- Market-data prices (`bid`, `ask`, trendbar `open/high/low/close`) are **integers in pipettes**: `display = raw / 10^pipDigits`.
  Reference precision table: XAUUSD pipDigits 3, BTCUSD 2 — broker-dependent, verify.
- Rate limits: general 50 req/s; historical (`get_trendbars`, history tools) 5 req/s.
- Errors may be plain strings (not JSON envelopes). Treat both.

## 3. Hard safety: read-only allowlist

```python
ALLOWED_TOOLS = frozenset({"get_version", "get_symbols", "get_spot_prices", "get_trendbars"})
```
- The adapter's single `call(tool, args)` raises `ForbiddenTool` for anything else — no override flag, no env switch.
- Keep a `KNOWN_TRADING_TOOLS` constant (`create_order, amend_order, cancel_order, amend_position, close_position`) used only
  for detection; a test greps the codebase to ensure those names appear nowhere else.
- If `tools/list` shows trading tools, send one Telegram warning recommending a data-profile token (functionality unchanged).
- Test against a fake cTrader MCP server that exposes trading tools and records calls: after all adapter tests, the fake
  must have recorded zero calls to them.

## 4. Credentials

- Sources, in precedence order: `kv.ctrader_override` (set by Telegram `/ctrader`) → `CTRADER_MCP_CONFIG` →
  `CTRADER_MCP_URL` + `CTRADER_MCP_TOKEN`.
- Parse pasted configs robustly: JSON of any shape (search recursively for a URL under keys like `url`, `serverUrl`,
  `httpUrl`, `endpoint`, and for an `Authorization` header / `token` / `bearer` value), or a bare token (reuse the known URL).
  Accept `Bearer xxx` or `xxx`. Never log the token; mask as `abcd…wxyz`.
- Hot swap: new credentials ⇒ close client, reconnect, run selftest, report in Telegram. No redeploy needed.

## 5. Client behaviour

- One long-lived MCP client session over streamable HTTP (`httpx2.AsyncClient` with `Authorization` header,
  connect timeout 10 s, read timeout 20 s). On transport/session errors: close, reconnect with backoff 1, 2, 5, 10, 30 s.
- Global limiter: ≤ 20 req/s general, ≤ 4 req/s historical, concurrency 2.
- **Auth error classification**: HTTP 401/403, or error text matching `unauthori|forbidden|expired|invalid token|session`
  (case-insensitive) ⇒ `AuthError` ⇒ health `auth_error`, PAUSED, immediate Telegram alert with renewal steps.
  Other failures ⇒ `DataError` ⇒ outage logic (validation-rules §12).
- Startup discovery: `get_version`, `tools/list` (store names + input schemas in `kv`), `get_symbols` ⇒ map `SYMBOLS` via
  `SYMBOL_MAP` → exact name → case-insensitive exact → startswith (e.g. suffixed names); ambiguous ⇒ not resolved + alert.
  Read the real parameter names/enums from the stored input schemas and adapt argument building to them.

## 6. Decoding and calibration (prevents the pipettes foot-gun)

1. Digits from symbol metadata if a digits-like field exists; else `PRICE_DIGITS` override; else precision-table default.
2. Decode one live bid. It must fall inside `PRICE_BANDS[symbol]`. If not, try exponents 0…8; exactly one inside the band ⇒
   use it and warn "auto-calibrated"; none ⇒ symbol disabled + alert "set PRICE_DIGITS".
3. If a numeric field already has a fractional part, treat it as display price (still band-checked).
4. Cross-check: last M1 close vs bid within 1 %; otherwise alert and disable the symbol.
5. Round only for display (2 decimals); keep full precision internally.

## 7. Fetch strategy

- Quotes: every `QUOTE_POLL_S` for symbols with active setups or a snapshot in the last 5 min (one batched call).
- Candles: right after `t + tf + CLOSE_GRACE_S`, fetch a small window (last ~3 bars) per needed timeframe; merge by open
  timestamp; drop any bar whose close time is in the future (forming bar). Arming/snapshot: bigger windows, chunked by 720 h.
- Candle timestamps are open times (verify in selftest: newest M1 bar of a live market starts in the current or previous
  minute); keep D1/W1 boundaries exactly as returned (do not assume midnight UTC).
- Idle: `get_version` every 5 min to catch expired credentials before the owner needs the system.

## 8. `/selftest` report (Telegram, Albanian, ✅/❌ per line)

credentials source + masked token · `get_version` build · profile (data/trading) · symbols resolved (id, name, digits,
calibration) · live quote per symbol (bid/ask/spread/age) · one closed bar per timeframe M1…D1 with NY time and
alignment check · clock skew (quote timestamp vs server clock, warn > 5 s) · Telegram ok · volume writable ·
public URL + OAuth metadata reachable · news feed status · active profile (STRICT/BALANCED).
