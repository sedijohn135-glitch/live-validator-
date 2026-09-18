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
| D11 | The shadow comparison adds a fourth value, `UNRESOLVED`, for a blind-limit fill that reached neither target nor stop inside the window | `validation-rules.md` §11 lists only WIN/LOSS/NO_FILL; calling an unresolved fill "no fill" would distort the saves count, and `UNRESOLVED` is simply not counted |
| D15 | Argument names for `get_spot_prices` and `get_trendbars` are read from the live `tools/list` schema instead of the names in `ctrader-remote-mcp.md` §2 | the live account's rest-proxy names the batch parameter `symbolId` (still an array), and the reference says the live server wins. `ARG_ALIASES` tries the documented name first, then the known variants, and `_fit_type` wraps or unwraps a value to match the declared array-ness |
| D16 | A `get_trendbars` window bound is formatted from the schema: epoch milliseconds when the type is numeric, otherwise ISO-8601 Z, falling back once to the epoch number as a string | the live account declares `fromTimestamp` as a string and rejects the number; `ctrader-remote-mcp.md` §2 says both forms exist, so the adapter tries one, reads the validation error and flips, instead of the owner debugging a proxy build |
| D17 | History requests are chunked to at most 90 bars per call, not only to the documented 720-hour window | the live proxy truncates a response at about 100 bars whatever the window, so a 33-hour M5 request returned 8 hours. That left `asian_high/low` and `ny_midnight_open` empty (v11 §3.1, §1.3, §3.4) and made the 24-hour liquidity lookback of G-15/T-01 blind. The history is now warmed in the background at startup so the first `market_snapshot` does not pay for the extra calls |
| D18 | G-02 accepts a price derived from the newest M1/M5 close when the tick feed hiccups (up to `INTAKE_QUOTE_MAX_AGE_S`, 180 s), while T-07 refuses a synthetic price outright | arming only starts monitoring, so a dropped tick should not throw away a good setup; entering is a different decision and still demands a real quote no older than five seconds, so "no false ENTER" is untouched |
| D19 | G-15 placement checks only the side of the entry (SSL below a long, BSL above a short) instead of requiring the pool to sit between stop and entry as `validation-rules.md` §3 G-15 does | v11 §5.8 asks only that the opposing pool was taken before entry. A PM continuation shorts an inversion array hours after the morning swept the PDH, so the pool is routinely far beyond the stop; the reality and sweep checks, plus G-07's range cap, still do the real work |
| D20 | `LUNCH_MACRO_PM` runs 13:30–16:00 in both profiles, not 14:00–15:00 in STRICT | v11 §3.10 Step 5 works the PM inversion arrays through the Last Hour to the cash close, so a 14:55 submission had four minutes of window and failed G-18. Silver Bullet is deliberately untouched: v11 Iron Rule 13 says 15:00–16:00 is not a Silver Bullet window |
| D21 | `AUTH_PATTERN` requires an auth word next to a credential; "session" and "expired" alone no longer count | a broker answering "trading session is closed" during the daily 17:00–18:00 break was classified as an expired token, which paused the engine and sent a false 🔑 alarm every day |
| D22 | the 🔑 token warning repeats hourly while the token is dead, and `/selftest` and `/status` print the cTrader account number read from the token | the old fixed dedupe key alerted once for the lifetime of the database, so a second expiry weeks later was silent; and switching account in cTrader invalidates the token without saying so, which looked like an outage |
| D23 | `/selftest` and `candles_held` report H4 too, but `usable` still gates only on M1/M5/M15/H1/D1 | H4 feeds the G-15 liquidity swings and the H4 FVGs, so its bar count belongs in the health report; it is not in the per-tick refresh set because no live trigger reads it, and gating on it would let a thin H4 history refuse an otherwise complete snapshot |
| D24 | the failure reason is printed whenever the feed is not `ok`, in the snapshot data block, in `/health` and in `/status` | the owner cannot read Railway logs; a `down` status with no reason left both the owner and the model guessing, and the snapshot hid the reason entirely whenever cached bars kept it usable |
| D25 | a dead streamable-HTTP session ("Session not found", 404, "re-initialize") counts as transient, and the engine forces a reconnect every 120 s while the feed is down | the session id died overnight and was reused for hours: every call failed and the owner got the same outage message every hour from 01:44 to 04:45 with no recovery until a restart |
| D14 | Every tool is advertised with `readOnlyHint=True`, including `setup_submit` and `setup_cancel`, instead of the write annotations `mcp-oauth.md` §5 prescribes | Gemini asks for a confirmation tap on anything it reads as a write, which turns every analysis into two steps; the owner asked for the flow to run without it. Neither tool moves money — they arm or stop the monitoring of one setup on the owner's own service — and the ENTER message on Telegram, not the tool call, is what he acts on |
| D13 | `MCP_AUTH=open` serves `/mcp` with no authentication at all, and the OAuth routes are then not registered | the owner asked for the behaviour his previous server had: Gemini connects straight from the URL with no login page. The trade-off (anyone with the address can call `setup_submit` and trigger a false ENTER on his phone) was put to him and he chose it. The default stays `oauth`, the mode is visible in `/health`, `/status` and `/selftest`, and the switch is one Railway variable to remove |
| D12 | Shadow outcomes are evaluated from the in-memory M1 history (≈ 25 hours) when `/stats` or the daily report runs, not with dedicated chunked `get_trendbars` backfills | the horizon is 24 hours, so the retained history already covers it, and the alternative spends historical rate limit on a statistic |

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
