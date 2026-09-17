# live-validator — spec index

One Railway service that is three things at once:

1. an OAuth-protected remote MCP server giving Gemini Spark read-only IC Markets cTrader data,
2. a 24/7 monitor that validates or invalidates ICT v11 setups with deterministic rules,
3. a Telegram notifier that sends `HYR TANI` (enter) or `MOS HYR` (do not enter) to the owner.

Success is measured by two things only: an ENTER tends to work out, and a block would have lost.

## Where the requirements live

| Topic | Source |
|---|---|
| Mission, phases, definition of done | `.claude/skills/live-validator-builder/SKILL.md` |
| Process layout, stack, persistence, routes | `references/architecture.md` |
| Every rule, threshold and golden scenario | `references/validation-rules.md` |
| cTrader adapter, allowlist, decoding | `references/ctrader-remote-mcp.md` |
| MCP tools, OAuth, schema limits | `references/mcp-oauth.md` |
| Albanian message templates | `references/telegram-messages-sq.md` |
| Owner docs | `references/deploy-railway-sq.md`, `references/gemini-v11-addendum.md` |
| Risks to tick before shipping | `references/failure-modes.md` |

## Module map

| Module | Responsibility |
|---|---|
| `app/config.py` | env parsing, STRICT/BALANCED profiles, per-symbol settings |
| `app/timeutil.py` | New York time, kill zones, macros, market hours, closed-candle rule |
| `app/market.py` | candles, ATR, swings, FVGs, session levels, snapshot builder |
| `app/setup_model.py` | the `setup_submit` payload and its normalisation |
| `app/context.py` | the market context injected into every rule |
| `app/rules.py` | G-01…G-18 intake, L-01…L-04 invalidation, T-02…T-08 triggers, score |
| `app/engine.py` | state machine, model overrides, outcomes, shadow tracking, stats |
| `app/ctrader.py` | cTrader Remote MCP client, read-only allowlist, decoding, rate limits |
| `app/telegram.py` | Albanian templates, outbox sender |
| `app/news.py` | optional USD news blackout (fail-open) |
| `app/oauth.py` | SQLite OAuth 2.1 provider and the login page |
| `app/tools.py` | the six MCP tools with flat schemas |
| `app/runtime.py` | polling, engine tick, background tasks, `/health`, `/selftest`, commands |
| `app/main.py` | MCP server, routes, lifespan, ASGI app |
| `app/store.py` | SQLite schema, transactions, outbox, lease |

## Decisions & deviations

| # | Decision | Why |
|---|---|---|
| D1 | `app/runtime.py` added beside the modules listed in `architecture.md` §4 | the scheduler, Telegram loops, `/health` and `/selftest` would have doubled the size of `engine.py`; the layout stays flat |
| D2 | Optional tool parameters use sentinel defaults (`0.0` for prices, `""` for text) instead of nullable types | Gemini's function calling rejects `anyOf`/`null`; `mcp-oauth.md` §5 requires primitives only, and a price of exactly 0 is impossible for these symbols |
| D3 | The daily 17:00–18:00 New York break applies to BTCUSD as well as XAUUSD | IC Markets pauses crypto CFDs too; being wrong here only ever costs a missed trade, never a bad entry |
| D4 | An FVG counts as `inverted` when a body closed beyond its far edge and price later re-entered the zone | `validation-rules.md` names the status but not a machine test; this is the narrowest reading that stays checkable |
| D5 | `TriggerState.tap_ts` is the open time of the bar holding the tap extreme | T-08 walks the ltf grid from it, so an off-grid timestamp would report every bar missing |
| D6 | Trading-tool names appear once, in `KNOWN_TRADING_TOOLS` | a grep test enforces it, so no code path can ever name one |
| D7 | US bank holidays are a small built-in table; the feed is preferred when it is up | the free calendar feed is unofficial and may be down, and P-4 is a one-point score penalty |
| D8 | `/health` reports `version` as the package version and the active profile | Railway's healthcheck must answer 200 even with no credentials at all |
| D9 | `MISSED` is reserved for the cases `validation-rules.md` §7 names (spread spike over two bars, price ran away, confirmation during an outage, active pause); T-01…T-05, T-08 and T-09 only hold the setup in `IN_ZONE` until it expires | a stale tap can still be refreshed by a new extreme, so ending the setup early would throw away a valid trade |
| D10 | Expired `oauth_pending`, old `oauth_codes` and stale per-IP `login_attempts` rows are purged whenever a new authorization starts | the volume is small and these tables would otherwise grow for ever |

## Test map

| Gate | Tests |
|---|---|
| Primitives | `tests/test_timeutil.py`, `tests/test_market.py`, `tests/test_config.py`, `tests/test_store.py` |
| Intake rules | `tests/test_rules.py` (a passing and a failing case per rule) |
| Engine | `tests/test_golden.py` (all 17 golden scenarios), `tests/test_engine.py` |
| Telegram | `tests/test_telegram.py` |
| cTrader | `tests/test_ctrader.py` against `tests/fake_ctrader.py` (which also exposes trading tools) |
| MCP tools | `tests/test_tools.py` (schema lint, snapshot size, submit → ENTER) |
| OAuth and HTTP | `tests/test_app.py` |
| News | `tests/test_news.py` |
| Docs | `tests/test_docs.py` (addendum names match the registered tools; no Railway config files) |
| Failure modes | `tests/test_failure_modes.py`, ticked row by row in `docs/FAILURE_MODES.md` |
