# Architecture

## 1. Decision: ONE MCP server (two tool groups), not two servers

The owner asked "1 MCP with both, or data and validation separately?". Decision: **one deployed service exposing one
MCP endpoint**, internally split into two modules (`market` and `validator`). Reasons, in order of weight:

1. **Same data for analysis and monitoring.** If Gemini analyses data from server A and the monitor watches server B,
   candle boundaries, decoding or timing can differ → levels "valid" in the analysis get falsely (in)validated live.
   One adapter + one decoder = zero divergence.
2. **Gemini Spark connects to custom MCP servers only via OAuth 2.1** (URL + dynamic client registration or client
   id/secret). The cTrader Remote MCP uses a static per-account bearer token, so Gemini cannot use it directly anyway —
   a wrapper is required, and the wrapper is naturally the same service.
3. **Safety.** cTrader Remote MCP exposes trading tools. Gemini must never see them. Our server re-exposes only
   read-only data.
4. **Phone-only owner.** One Railway service, one URL, one password, one Gemini link, one bill.

The cTrader Remote MCP is therefore NOT connected to Gemini. Only `live-validator` is.

## 2. Data flow

```
 Owner (phone) ──"xauusd"──► Gemini Spark (v11 skill + addendum)
                                   │  OAuth 2.1 bearer
                                   ▼
                     https://<railway-domain>/mcp  (live-validator, 1 process)
          ┌────────────────────────────────────────────────────────────────┐
          │ MCP tools: market_snapshot, market_candles, validator_rules,    │
          │            setup_submit, setup_status, setup_cancel             │
          │                     │                     │                     │
          │              market module          validator module            │
          │   (cTrader client, candles,      (intake gate, state machine,   │
          │    decoding, levels, FVG scan)    triggers, outcomes, stats)    │
          │                     └───────► SQLite on Railway volume ◄────────┤
          │ background tasks: poller/engine · telegram sender · telegram    │
          │                   commands · news cache · daily report · lease  │
          └───────────────┬──────────────────────────────────┬─────────────┘
                          │ bearer token (read-only allowlist)│ Bot API
                          ▼                                   ▼
            cTrader Remote MCP (IC Markets)            Telegram → owner's phone
```

## 3. Stack (pin exact versions in `uv.lock`)

- Python **3.12** (`python:3.12-slim` image). Package manager **uv** (`uv sync --frozen`).
- `mcp==2.2.0` (verified: server class `mcp.server.mcpserver.MCPServer`; HTTP client library is `httpx2`, not `httpx`;
  details in `mcp-oauth.md`). Its dependencies bring `starlette`, `uvicorn`, `pydantic`, `anyio`, `httpx2`.
- `tzdata` (mandatory: slim images may lack the tz database → `ZoneInfoNotFoundError` for `America/New_York`).
- stdlib `sqlite3` (WAL mode) — no ORM. Telegram via plain HTTP calls with `httpx2` — no bot framework.
- Dev: `pytest`, `ruff`, `httpx` (only for Starlette `TestClient`). Use anyio's pytest plugin (no pytest-asyncio).
- Single uvicorn worker. Start command (Dockerfile, shell form so `$PORT` expands):
  `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips '*' --workers 1`
- Dockerfile runs as **root** (Railway volumes are mounted as root; a non-root user causes permission errors).
- **No `railway.json` / `railway.toml`**: Railway config-as-code is deprecated and new services cannot opt in.

## 4. Repository layout (keep it this flat)

```
app/
  main.py        # builds MCPServer + routes + lifespan; exposes `app`
  config.py      # env parsing, profiles (STRICT/BALANCED), per-symbol settings, windows table
  store.py       # SQLite schema, migrations-by-version, queries, lease
  timeutil.py    # NY time, windows, macros, market hours, blackout checks
  market.py      # candle store, closed-candle logic, ATR, swings, FVG scan, levels, snapshot builder
  ctrader.py     # cTrader Remote MCP client: allowlist, discovery, symbols, decoding, rate limit, errors
  rules.py       # intake gate + live checks as pure functions
  engine.py      # scheduler, state machine, replay, outcomes, shadow tracking, stats
  telegram.py    # templates (sq), outbox sender, command poller
  news.py        # optional economic calendar cache (fail-open)
  oauth.py       # SQLite OAuth provider + login routes
  tools.py       # MCP tool registration
tests/           # unit, scenario (golden), integration (MCP, OAuth, fake cTrader, fake Telegram)
docs/            # SPEC.md, SETUP_SQ.md, GEMINI_V11_ADDENDUM.md, RULES_SQ.md
Dockerfile  pyproject.toml  uv.lock  .env.example  .gitignore  README.md  CLAUDE.md
.github/workflows/test.yml
```

## 5. Runtime tasks (started once, from the MCPServer lifespan)

In SDK 2.2.0 the streamable-HTTP session manager enters the server lifespan **once** at startup (verified in source).
Still add a test that background tasks start exactly once across many MCP requests.

1. **Lease** — row `lease('engine')` with holder id + heartbeat every 10 s; a second instance (deploy overlap) must not
   run engine/Telegram poller while a fresh lease (<30 s) exists. Prevents double messages and Telegram 409 conflicts.
2. **Market poller + engine tick** (1 s loop): quotes every `QUOTE_POLL_S` for symbols with active setups or a recent
   snapshot; candle fetch right after each needed timeframe closes; dispatch closed-candle events to the engine.
   When nothing is active: heartbeat call every 5 min to detect expired credentials early.
3. **Telegram sender** — drains `outbox` (at-least-once, dedupe key unique), handles 429 `retry_after`.
4. **Telegram command poller** — long polling `getUpdates` (no webhook), owner chat only.
5. **News cache** — refresh every 4 h (optional, fail-open).
6. **Daily report** — 17:05 New York time.

## 6. Persistence (SQLite at `DATA_DIR/validator.db`, WAL, `busy_timeout=5000`)

- `setups` (id, symbol, direction, model, state, payload_json, computed_json, fingerprint, created_at, armed_at,
  tap_at, triggered_at, closed_at, expires_at, last_processed_at, entry_price, score, outcome, shadow_json)
- `events` (setup_id, ts, type, data_json) — full audit trail of every transition and reason
- `outbox` (dedupe_key UNIQUE, chat_id, text, created_at, sent_at, attempts, last_error)
- `oauth_clients`, `oauth_pending` (login transactions), `oauth_codes` (hash), `oauth_tokens` (hash, kind, family, …)
- `kv` (ctrader credential override, generated secret key, tool snapshot, symbol cache, spread samples summary)
- `lease`
- Schema version in `kv`; migrations are idempotent `CREATE TABLE IF NOT EXISTS` + `ALTER` guarded by version.

**Critical ordering:** a state transition and its outbox row are written in ONE transaction. The sender only reads
the outbox. Restarting at any moment can therefore never produce a second ENTER, and never lose one.

`DATA_DIR` = env `DATA_DIR` → else `RAILWAY_VOLUME_MOUNT_PATH` → else `./data`. If the path is not a mounted volume
(no `RAILWAY_VOLUME_MOUNT_PATH` while `RAILWAY_ENVIRONMENT` is set), show a loud warning in `/health`, `/status` and
one Telegram message: setups and the Gemini link would be lost on redeploy.

## 7. Configuration (Railway Variables)

| Variable | Required | Default / note |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | from @BotFather |
| `TELEGRAM_CHAT_ID` | yes* | empty at first; bot replies to `/start` with the id to paste |
| `OWNER_PASSWORD` | yes | ≥ 12 chars; used only on the Gemini link login page |
| `CTRADER_MCP_CONFIG` | yes* | paste the whole config copied from cTrader Web → Settings → Remote MCP |
| `CTRADER_MCP_URL` + `CTRADER_MCP_TOKEN` | alt | alternative to `CTRADER_MCP_CONFIG` |
| `PUBLIC_BASE_URL` | no | `https://$RAILWAY_PUBLIC_DOMAIN`; no trailing slash (normalize) |
| `DATA_DIR` | no | see §6 |
| `VALIDATOR_PROFILE` | no | `STRICT` (or `BALANCED`) |
| `NEWS_FILTER` | no | `on` |
| `NEWS_FEED_URL` | no | `https://nfs.faireconomy.media/ff_calendar_thisweek.json` (unofficial; fail-open) |
| `BTC_WEEKEND_ENTRIES` | no | `false` |
| `SYMBOLS` | no | `XAUUSD,BTCUSD` |
| `SYMBOL_MAP`, `PRICE_DIGITS`, `PRICE_BANDS`, `MAX_SPREAD` | no | JSON overrides per symbol |
| `OAUTH_STATIC_CLIENT_ID`, `OAUTH_STATIC_CLIENT_SECRET`, `OAUTH_STATIC_REDIRECT_URIS` | no | fallback if Gemini's dynamic registration fails |
| `LOG_LEVEL` | no | `INFO` |

(*) The service must boot and serve `/health` without these; features that need them report "not configured".
Credentials set later through Telegram (`/ctrader …`) are stored in `kv` and take precedence over env until
`/ctrader reset`.

## 8. HTTP routes

- `POST/GET /mcp` — MCP streamable HTTP (stateless, JSON responses), OAuth bearer required.
- OAuth (SDK-provided): `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp`,
  `/authorize`, `/token`, `/register`, `/revoke`.
- `GET/POST /oauth/login` — password page (custom route).
- `GET /health` — always 200 while the process runs: `{"ok":true,"version":…,"data":{"ctrader":"ok|auth_error|down|not_configured","last_quote_age_s":…},"telegram":…,"volume":…,"active_setups":…}`. Never include secrets.

## 9. Telegram commands (owner chat only; others ignored silently)

`/start` (shows chat id if not configured) · `/help` · `/status` · `/active` · `/cancel ID` · `/pause` · `/resume` ·
`/stats [7|30]` · `/selftest` · `/ctrader CONFIG_OR_TOKEN` · `/ctrader reset` · `/revoke_all` (Gemini tokens) · `/rules`

`/ctrader` messages contain a secret: after processing, delete the owner's message (`deleteMessage`), then reply with
a masked confirmation and the result of a live selftest.
