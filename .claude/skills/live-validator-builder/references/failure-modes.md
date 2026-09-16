# Failure modes — tick every row before shipping

Each row: what breaks in real life → required mitigation → proof (test or documented check).

## Deployment / platform

| # | Risk | Mitigation | Proof |
|---|---|---|---|
| D1 | App binds 127.0.0.1 or a fixed port → Railway healthcheck "service unavailable" | bind `0.0.0.0:$PORT` (shell-form CMD) | start test with `PORT=8123` |
| D2 | MCP SDK default host allowlist → Gemini gets 421 | explicit `transport_security` for public host | Host-header test (401 not 421) |
| D3 | `railway.json` ignored/deprecated for new services | none created; settings via dashboard, documented in SETUP_SQ | grep test |
| D4 | No volume → OAuth link, setups, outbox lost on redeploy | `DATA_DIR` resolution + loud warning | unit test of resolution + warning text |
| D5 | Non-root Docker user → volume permission denied | run as root | Dockerfile review |
| D6 | `/health` fails when cTrader creds missing/expired → deploy fails → owner can't fix | `/health` always 200 with status fields | test without env vars |
| D7 | `ZoneInfoNotFoundError` in slim image | `tzdata` dependency | test imports NY zone in clean env |
| D8 | Two instances during redeploy → double messages / Telegram 409 | DB lease; Telegram poller only under lease | lease test with two engines |
| D9 | Railway "serverless/app sleeping" stops monitoring | documented OFF in SETUP_SQ; `/status` shows uptime | doc check |
| D10 | Unpinned deps change APIs | `uv.lock` + `--frozen` | CI uses frozen sync |
| D11 | Claude Code pushed to a side branch → Railway never deploys | end on `main` or PR + Albanian merge steps | final message |
| D12 | Crash loop on bad env value | config parsing never raises at import; errors surface in `/health` + Telegram | test with garbage env |

## Gemini / MCP / OAuth

| # | Risk | Mitigation | Proof |
|---|---|---|---|
| G1 | Gemini can't use nested/anyOf schemas → tool unusable | flat schemas only | schema-lint test |
| G2 | Gemini registers with unexpected scopes → DCR 400 | `valid_scopes=None`, `required_scopes=None` | DCR test with scope "openid email" |
| G3 | Token not bound to resource → 401 after link | provider binds `BASE/mcp` when `resource` absent | OAuth test without resource |
| G4 | Issuer mismatch from trailing slash / http vs https | `BASE` normalised; never derive from request | metadata test |
| G5 | Link lost after every redeploy | OAuth tables in SQLite on volume | restart persistence test |
| G6 | Access token expires, refresh race breaks link | 24 h access, rotation with 10 min grace | refresh retry test |
| G7 | Brute force on login page | per-IP + global lockout, CSRF, constant-time compare, Telegram alert | lockout test |
| G8 | Gemini retries `setup_submit` → duplicate setups | fingerprint dedupe (G-19) | golden test 15 |
| G9 | Gemini invents prices from memory | G-06/G-07 drift & range guards; tool descriptions | golden test 8 |
| G10 | Gemini gets timezone math wrong | snapshot prints NY times; submit takes `pda_formed_at_ny` | parse tests incl. DST |
| G11 | Gemini write confirmation surprises owner | documented (one tap) | SETUP_SQ + addendum |
| G12 | Snapshot too large / slow | fixed counts, compact arrays, 15 s cache | size test < 60 KB |
| G13 | `/mcp/` vs `/mcp` redirect breaks clients | document exact URL `https://<domain>/mcp`; test both | route test |
| G14 | Background engine started per request | lifespan once | 20-request test |

## cTrader data

| # | Risk | Mitigation | Proof |
|---|---|---|---|
| C1 | Trading tool called by bug | hard allowlist, no override | fake server records zero trading calls |
| C2 | Pipettes decoded with wrong digits → nonsense levels | metadata/override/band calibration + M1 cross-check | decoding tests |
| C3 | One bad symbolId empties the whole quote batch | validate ids before batching | test with unknown id |
| C4 | Unsupported period (M_2, H_2…) → -32602 | only the 9 periods; map at startup from schema | test |
| C5 | >720 h window error | chunking | test with 250 D1 bars |
| C6 | Forming bar treated as closed | closed-candle rule + future-close drop | unit tests |
| C7 | Session/token expiry during monitoring | AuthError → PAUSED + alert + `/ctrader` hot swap + replay | outage golden test 5 |
| C8 | Rate limit 429 | limiter + backoff | test with fake 429 |
| C9 | Plain-string errors not parsed | classify text and JSON | tests for both |
| C10 | Symbol names differ/suffixed | resolution order + `SYMBOL_MAP` + selftest | tests |
| C11 | Price feed ≠ owner's execution platform | messages say "IC Markets cTrader"; SETUP_SQ advises executing on the same broker feed | doc |
| C12 | Market closed/holiday misread as outage | market-hours schedule; single holiday notice | test weekend |
| C13 | Timestamp semantics (open vs close) wrong | selftest alignment check; open-time assumption tested with fake | test |

## Engine / validation

| # | Risk | Mitigation | Proof |
|---|---|---|---|
| E1 | ENTER sent twice (restart, retry) | transition + outbox in one transaction; unique dedupe key | golden test 6 |
| E2 | Late ENTER after outage | replay turns triggers into MISSED (90 s exception) | golden test 5 |
| E3 | ENTER on stale quote | quote age ≤ 5 s at decision | unit test |
| E4 | Spread spike at rollover/news → bad fill | T-06 + time blocks + news blackout | golden test 14 |
| E5 | Price ran away before owner reads message | chase_limit in message + ENTER valid 5 min | message test |
| E6 | Wick noise invalidates good setups | body-close rules on respect_tf, wick allowed to CE | golden tests 2, 11 |
| E7 | DST shifts kill zones | zoneinfo; DST tests | golden test 16 |
| E8 | Same-candle TP/SL ambiguity inflates stats | conservative SL-first | unit test |
| E9 | Friday close / weekend gap for gold | Friday cutoff, expiry at close | test |
| E10 | BTC weekend chop | weekend entries off by default | test |
| E11 | Float precision / rounding changes decisions | compare full precision; round only in text | unit test |
| E12 | Clock skew | compare with quote timestamps; warn > 5 s | selftest |
| E13 | News feed down blocks everything | fail-open + warning line | test |

## Telegram

| # | Risk | Mitigation | Proof |
|---|---|---|---|
| T1 | "can't parse entities" with HTML | `parse_mode=HTML` + escape every dynamic value | test with `<`, `&` in reasons |
| T2 | Message > 4096 chars | split on line boundaries | test |
| T3 | 429 flood control | honour `retry_after`; outbox retry with backoff | fake API test |
| T4 | Bot can't message owner before `/start` | SETUP_SQ step; `/start` shows chat id | doc + test |
| T5 | Strangers command the bot | only `TELEGRAM_CHAT_ID`; silent ignore | test |
| T6 | Token pasted in chat stays visible | delete `/ctrader` message after reading | test (fake API receives deleteMessage) |
| T7 | Secrets in logs | redaction filter for tokens/passwords/Authorization | log capture test |
