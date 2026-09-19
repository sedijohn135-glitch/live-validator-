# live-validator — spec index

One Railway service that is three things at once:

1. an OAuth-protected remote MCP server giving Gemini Spark read-only IC Markets cTrader data,
2. a 24/7 **universal validator** that watches any submitted setup and decides the moment of entry,
3. a Telegram notifier that sends `HYR TANI`, `LIMIT`, `SIGURO FITIMET` and the closing messages.

The validator judges live evidence, never the idea. It rejects nothing: see `docs/VALIDATOR.md` for
the contract, the signals and the maths.

## Where the requirements live

| Topic | Source |
|---|---|
| The validator's contract, signals, recalculation and protection rules | `docs/VALIDATOR.md` |
| What the owner sees, in Albanian | `docs/RULES_SQ.md` |
| How any prompt submits a setup | `docs/GEMINI.md` |
| Install and operate | `docs/SETUP_SQ.md` |
| Risks ticked before shipping | `docs/FAILURE_MODES.md` |
| Original build brief (historical, v11) | `.claude/skills/live-validator-builder/SKILL.md` |

## Module map

| Module | Responsibility |
|---|---|
| `app/config.py` | env parsing, operational profile, per-symbol settings |
| `app/timeutil.py` | New York time, session windows for the snapshot, market hours, closed-candle rule |
| `app/market.py` | candles, ATR, swings, FVGs, session levels, snapshot builder |
| `app/setup_model.py` | the permissive setup: repairs, never rejections |
| `app/context.py` | the market context the validator reads |
| `app/evidence.py` | the live evidence signals, the score and the holds |
| `app/plan.py` | entry, stop, targets and the profit-securing level |
| `app/engine.py` | the state machine and every Telegram transition |
| `app/ctrader.py` | cTrader Remote MCP client, read-only allowlist, decoding, rate limits |
| `app/telegram.py` | Albanian templates, outbox sender |
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
| D26 | v2: the validator became universal — every strategy rule (time, kill zone, premium/discount, liquidity, bias, checklist, expiry, news) was deleted, together with `app/rules.py`, `app/news.py` and the v11 payload | the owner's instruction: "validues live" only. A validator that refuses setups is a gatekeeper, and the refusals were rejecting ideas it had no business judging. What replaced them: `app/evidence.py` (live confirmation at the zone), `app/plan.py` (recomputed stop, targets and the profit-securing level) and exactly two cancellations. Rules and thresholds researched online and cited in `docs/VALIDATOR.md` |
| D27 | discovery and the history warm-up run in their own task, the engine loop starts immediately, and `/status` and `/health` report whether it is ticking | a live setup sat inside its own zone for 35 minutes without being processed: the loop awaited `_startup()` before its first pass, and loading the deep history (dozens of chunked calls, no timeout) kept it there. Nothing in the service said the engine was not running — the feed looked healthy because tool calls fetched their own data |
| D28 | the outbox skips a message the API refuses (after one plain-text retry) and drops it after 5 attempts, and `/status` reports a queue that stopped moving | `drain_once` returned on the first failure, so one message Telegram would not accept blocked every later alert permanently — the owner got no cancellation and nothing anywhere said why |
| D29 | every evidence signal has a materiality floor in ATR(M1); a reaction off the extreme is required; a confirmed setup in the expensive half of its own zone becomes a LIMIT in the better half; two closes through the zone reset the evidence | the owner objected that the engine could not tell a validating setup from a failing one, and he was right: a one-tick dip below the zone edge counted as a full liquidity sweep (2 points, primary), so ordinary chop inside a zone confirmed itself |
| D30 | a dropped Telegram message is recorded as dropped, never as sent; `outbox_health` counts drops, `setup_status` lists every message a setup should have produced, and `/selftest` names the sending bot and the destination chat | "no alert arrived" and "the API accepted it" looked identical from inside: a message abandoned after 5 attempts was marked sent, and `ok` from sendMessage says nothing about which bot sent it or which conversation received it |
| D31 | the zone touch and both cancellations are judged on the range price covered since the previous pass (the closed M1 bars plus the last seen price), not on the poll's own price; when a cancellation window also crossed the zone, the message says so | BTC-0919-NFP6 was cancelled at the stop having never reported the zone it had walked through: a poll sees a point, and a 40-point band on BTC is crossed between two passes — or behind a data outage — without any poll ever landing inside it. The owner got a registration and a cancellation and nothing in between |
| D32 | when the feed returns, M1 and M5 are refetched over the blind period (180 bars) before the next pass | the restore message promises the missing period was checked; with the routine three-bar refresh the hole stayed, and the range the setups are now judged on lived inside it |
| D33 | a cTrader call is retried up to three times with a 0.25 s / 0.75 s backoff; the generic SDK wording ("Server returned an error response") and the retryable HTTP codes count as transient, and only a dead session reconnects | the owner's feed kept dropping on a sentence the SDK prints for any non-2xx status, with no code and no body. It matched nothing in the transient list, so one bad gateway read as a verdict on the request and paused the monitor. The v6 JavaScript build the owner compared it to retried 408/429/5xx twice before surfacing anything, which is why its feed looked flawless on the same token |
| D34 | the outage alert waits until the feed has been down for OUTAGE_PAUSE_S, and the "data restored" message is sent only for an outage that was announced | the alert says monitoring is paused while entries were still running, and the owner got three restore messages in four minutes with no outage message between them |
| D35 | the break of the nearest opposing demand (short) or supply (long) is required for any entry: without it the verdict holds on `STRUCTURE`. Every other signal substitutes freely for every other | the owner's rule, verbatim: *konfirmimi nearest demand supply break — pa këtë s'ka hyrje*. And the reason substitution is free: the analysis names a model and the signs it expects, but the market rarely gives exactly those signs. An engine that waits for the one sign the analysis predicted stays blind while a different, equally valid confirmation prints in front of it |
| D36 | two signals added, both primary: `AO_DIV` (Awesome Oscillator divergence, SMA5 − SMA34 of the median price, read on the whole M1 series) and `QUASIMODO` (left shoulder, higher head, neckline broken, price back at the shoulder — the return must come after the break) | the two confirmations the owner actually trades: price makes a higher high while AO does not, or the quasimodo right shoulder when there is no divergence to read. Neither existed in the engine, so a setup confirmed by either was invisible to it |
| D37 | three equal confirmations — `ZONE_BREAK`, `AO_DIV`, `QUASIMODO` — and any one of them is an entry: 1/3 konfirmim, 2/3 i fortë, 3/3 shumë i fortë. None is mandatory, none outranks another, and reclaim/rejection/momentum/absorption became supporting evidence that never produces an entry on its own | the owner's rule, refining D35 the same day: *nëse 1 nga këto shfaqet hyrje. nëse 2 akoma më e fortë, nëse 3 super e fortë*. The mandatory break of D35 and its `STRUCTURE` hold are gone: the break is now one of three equals |
| D38 | `ZONE_BREAK` is read on M1, M5 and M15, and when more than one has broken the level nearest the entry zone is the one reported | the nearest demand or supply can live on any of them — a short into an M15 supply is confirmed by the M15 demand giving way, and reading only M1 would miss it |
| D39 | `ZONE_BREAK` is required again and nothing substitutes for it; `AO_DIV` and `QUASIMODO` raise the strength of an entry the break already made and never make one | the owner wrote the strategy out in full and it settles the question D35 and D37 kept swapping. Step 3: *nëse nuk ka thyerje zone, anuloje çdo setup. Instant Entry është e ndaluar.* Step 2 on the divergence: *divergjenca NUK është sinjal hyrjeje, por paralajmërim* |
| D40 | a third cancellation, `CANCELLED_ZONE_BROKEN`: one candle closing beyond the far edge of the zone — the head of the quasimodo — ends the setup, where it previously reset the evidence and kept watching | strategy step 6: *nëse një qiri mbyllet jashtë zonës së QM, setup-i është i anuluar*. The head is the far edge and the stop sits a buffer beyond it, so this fires earlier than the stop and costs less; if the owner is already in, the message says to close manually |
| D41 | `market_snapshot` carries an `ao` block for M15 and M5: the last 10 oscillator values and the divergence they show, computed server-side | the owner asked whether Gemini can read AO without a chart. The value is exactly determined by the candles, so conceptually yes — but it means two moving averages over 34 bars, several times, worked out in prose, and that is the one part of this strategy an LLM cannot be trusted with. The engine already computed it for `AO_DIV`; now the same function serves the snapshot, so the analysis reads one number instead of deriving it |
| D14 | Every tool is advertised with `readOnlyHint=True`, including `setup_submit` and `setup_cancel`, instead of the write annotations `mcp-oauth.md` §5 prescribes | Gemini asks for a confirmation tap on anything it reads as a write, which turns every analysis into two steps; the owner asked for the flow to run without it. Neither tool moves money — they arm or stop the monitoring of one setup on the owner's own service — and the ENTER message on Telegram, not the tool call, is what he acts on |
| D13 | `MCP_AUTH=open` serves `/mcp` with no authentication at all, and the OAuth routes are then not registered | the owner asked for the behaviour his previous server had: Gemini connects straight from the URL with no login page. The trade-off (anyone with the address can call `setup_submit` and trigger a false ENTER on his phone) was put to him and he chose it. The default stays `oauth`, the mode is visible in `/health`, `/status` and `/selftest`, and the switch is one Railway variable to remove |
| D12 | Shadow outcomes are evaluated from the in-memory M1 history (≈ 25 hours) when `/stats` or the daily report runs, not with dedicated chunked `get_trendbars` backfills | the horizon is 24 hours, so the retained history already covers it, and the alternative spends historical rate limit on a statistic |

## Test map

| Gate | Tests |
|---|---|
| Primitives | `tests/test_timeutil.py`, `tests/test_market.py`, `tests/test_config.py`, `tests/test_store.py` |
| The no-rejection contract | `tests/test_setup_model.py` |
| Live evidence | `tests/test_evidence.py` (each signal, the balance, every hold) |
| Prices and protection | `tests/test_plan.py` |
| Lifecycles | `tests/test_engine.py` (register → touch → ENTER/LIMIT → secure → close) |
| Telegram | `tests/test_telegram.py` |
| cTrader | `tests/test_ctrader.py` against `tests/fake_ctrader.py` (which also exposes trading tools) |
| MCP tools | `tests/test_tools.py` (schema lint, snapshot size, submit is never refused) |
| OAuth and HTTP | `tests/test_app.py` |
| Live feed incidents | `tests/test_feed.py` |
| Docs | `tests/test_docs.py` (the guides name only real tools and parameters; no Railway config files) |
| Failure modes | `tests/test_failure_modes.py`, ticked row by row in `docs/FAILURE_MODES.md` |
