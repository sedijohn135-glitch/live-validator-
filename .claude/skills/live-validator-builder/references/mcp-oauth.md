# MCP server + OAuth for Gemini Spark

## 1. What Gemini Spark needs (verified from Google's help page and field reports, Sept 2026)

- Custom apps are added in the Gemini **web app**: Settings → Connected Apps → Custom apps for Spark → enter the MCP
  server URL. The server must follow the standard MCP spec (remote, HTTPS).
- Authentication is **OAuth**: if the server supports Dynamic Client Registration it is automatic; otherwise the user
  enters client credentials under "Advanced features". Static bearer tokens are not an option → we implement an OAuth
  authorization server.
- Write actions from custom apps currently require **manual confirmation** by the user in Gemini (one tap). Expected;
  mark read-only tools correctly so only `setup_submit`/`setup_cancel` ask.
- Requirements on the Google side (owner's responsibility, document them): AI Pro or Ultra, personal account, 18+, US,
  Gemini language English, Keep Activity on.

## 2. Verified SDK facts (`mcp==2.2.0`) — read the installed source before coding anyway

- Server: `from mcp.server.mcpserver import MCPServer` (v1's `FastMCP` path is legacy; do not build on it).
  Constructor accepts `name, instructions, version, auth_server_provider, token_verifier, auth=AuthSettings(...), lifespan=...`.
- HTTP app: `server.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True, transport_security=..., host=...)`
  returns a Starlette app whose lifespan runs the session manager (which enters the server lifespan once).
  **Use this app as the root ASGI app**; add extra routes with `@server.custom_route(path, methods=[...])`.
  Do not mount it inside another Starlette app (its lifespan would not run).
- **Landmine:** `streamable_http_app(host="127.0.0.1")` is the default and auto-enables DNS-rebinding protection with
  `allowed_hosts` = localhost only ⇒ every request from Gemini (Host = railway domain) gets **421 Invalid Host header**,
  while local tests pass. Pass `transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)`
  in production (the endpoint is protected by OAuth bearer tokens; rebinding protection targets unauthenticated localhost
  servers) and keep the localhost-protected default only for local dev. Content-Type validation stays active regardless.
  Test: a request with `Host: example.up.railway.app` must get 401 (auth), never 421.
- Auth settings: `from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions`.
  Provider protocol: `from mcp.server.auth.provider import OAuthAuthorizationServerProvider, AuthorizationParams, AuthorizationCode, RefreshToken, AccessToken, AuthorizeError, TokenError, RegistrationError`.
  SDK routes: `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp`, `/authorize`, `/token`,
  `/register`, `/revoke`; 401 responses carry `WWW-Authenticate` with `resource_metadata`. PKCE S256 is enforced by the SDK.
  The SDK does not implement Client ID Metadata Documents — DCR + optional static client is enough.
- Client side (for cTrader): `mcp.client.streamable_http.streamable_http_client(url, http_client=httpx2.AsyncClient(...))`
  and the high-level `mcp.client.client.Client` / `ClientSession`. Headers go on the `httpx2.AsyncClient`.

## 3. Server wiring

```python
BASE = settings.public_base_url            # "https://xyz.up.railway.app" — no trailing slash (RFC 8414 exact match)
server = MCPServer(
    name="live-validator",
    instructions="Read-only IC Markets cTrader data + ICT v11 setup validator. Prices come only from market_snapshot.",
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=BASE,
        resource_server_url=f"{BASE}/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=None, default_scopes=["mcp"]),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=None,              # do not reject clients that request other scopes
        validate_token_resource=True,      # provider always binds tokens to BASE/mcp (see §4)
    ),
    lifespan=engine_lifespan,
)
app = server.streamable_http_app(json_response=True, stateless_http=True, transport_security=prod_security)
```

Why these choices: `valid_scopes=None` so Gemini's registration never fails on an unexpected scope string; stateless
JSON responses avoid long-lived SSE streams through Railway's proxy; `validate_token_resource=True` is safe because the
provider sets `resource` itself.

Uvicorn must run with `--proxy-headers --forwarded-allow-ips '*'`. Never build OAuth URLs from `request.url` —
always from `BASE`.

## 4. OAuth provider (SQLite, single owner)

- `get_client` / `register_client`: persist `OAuthClientInformationFull` JSON. Also return a static client from
  `OAUTH_STATIC_*` env when configured (fallback for Gemini's manual credentials path).
- `authorize(client, params)`: store a pending login transaction (random 32-byte id, params incl. state, scopes,
  code_challenge, redirect_uri, redirect_uri_provided_explicitly, resource; TTL 10 min) and return
  `f"{BASE}/oauth/login?tx={id}"`.
  Resource binding: `resource = params.resource or f"{BASE}/mcp"`; if provided and different after normalising a
  trailing slash ⇒ raise `AuthorizeError` (use the closest allowed error code in the SDK).
- `/oauth/login` GET: minimal mobile-friendly HTML (Albanian + English labels), password field, hidden tx, CSRF token =
  HMAC(secret, tx). POST: verify CSRF and tx TTL; rate limit per client IP (first `X-Forwarded-For` hop): 5 failures /
  15 min ⇒ 15 min lock; 20 failures / hour globally ⇒ 1 h lock + Telegram alert. Compare with `hmac.compare_digest`.
  On success: create an authorization code (≥ 32 random bytes, store SHA-256 only, TTL 5 min, single use), redirect 302
  to the registered redirect_uri with `code` and `state` (use the SDK's redirect helper). Telegram: "🔗 Gemini u lidh".
  If `OWNER_PASSWORD` is missing the page says so and never issues codes.
- `exchange_authorization_code`: mark used; a second use revokes the whole token family. Issue opaque tokens
  (store hashes): access TTL 24 h, refresh TTL 90 days, `resource` from the code.
- `exchange_refresh_token`: rotate; the previous refresh token stays usable for 10 min (network retries), then invalid.
- `load_access_token`: hash lookup, not expired, not revoked ⇒ `AccessToken(token, client_id, scopes, expires_at, resource)`.
- `revoke_token` revokes the family. Telegram `/revoke_all` revokes everything.
- Everything persists in SQLite ⇒ Gemini stays linked across redeploys (requires the volume).

## 5. Tool schema rules (compatibility with Gemini's function calling)

Gemini's tool-calling accepts a subset of JSON Schema. Unsupported constructs make tools fail silently or error.
Every tool input schema MUST be flat:
- top-level `type: object` with primitive properties only: `string` (optionally `enum`), `number`, `integer`, `boolean`;
- no nested objects, no arrays of objects, no `$ref`, `$defs`, `anyOf`, `oneOf`, `allOf`, `not`, no `null` types
  (optional = simply not listed in `required`), no `format` other than plain strings;
- every property has a one-line `description`.
Implement tools with plain typed parameters (not nested Pydantic models) and write a test that walks every registered
tool's `inputSchema` and fails on any forbidden construct. Results: return one JSON **text** block (compact, no output
schema) — `structured_output=False`.

Annotations: `readOnlyHint=True, openWorldHint=False` for `market_snapshot`, `market_candles`, `validator_rules`,
`setup_status`; `setup_submit`: `readOnlyHint=False, destructiveHint=False, idempotentHint=True`;
`setup_cancel`: `readOnlyHint=False, destructiveHint=True, idempotentHint=True`.

## 6. Tool contracts

**`market_snapshot(symbol)`** — symbol enum `XAUUSD|BTCUSD`. Returns (≤ ~60 KB):
```json
{"schema":"snapshot/1","symbol":"XAUUSD","source":"IC Markets cTrader (bid candles)",
 "time":{"utc":"…","ny":"2026-09-16 08:12","weekday_ny":"Wednesday","market_open":true,
         "active_windows":["NY_KZ","NY_2022"],"active_macro":"08:00","next_windows":[["AM_SB","10:00"],…],
         "lunch_block":false,"news_blackout":null,"upcoming_news":[["08:30","USD CPI"]]},
 "quote":{"bid":5654.52,"ask":5654.77,"spread":0.25,"age_s":1},
 "levels":{"asian_high":…,"asian_low":…,"london_high":…,"london_low":…,"ny_midnight_open":…,"six_am_open":…,
           "pdh":…,"pdl":…,"pwh":…,"pwl":…,"lookback_high":…,"lookback_low":…},
 "atr":{"M5":…,"M15":…,"H1":…,"D1":…},
 "swings":{"M15":{"highs":[["2026-09-16 06:45",5661.2],…],"lows":[…]},"H1":{…}},
 "fvgs":{"M5":[{"dir":"BULL","low":…,"high":…,"ce":…,"formed_at":"2026-09-16 07:35","kind":"wick|body","status":"untouched|tapped|ce_breached|failed|inverted"}],"M15":[…],"H1":[…],"H4":[…]},
 "candles":{"D1":[["2026-09-15 00:00",o,h,l,c],…],"H4":[…],"H1":[…],"M15":[…],"M5":[…],"M1":[…]},
 "notes":["times are New York","candle time = open time","use formed_at exactly in setup_submit"]}
```
Candle counts: D1 30, H4 60, H1 72, M15 96, M5 96, M1 60; last 6 FVGs per timeframe (not `failed` unless `inverted`),
last 5 swings per side. Cache per symbol for 15 s. Times printed in NY as `YYYY-MM-DD HH:MM`.

**`market_candles(symbol, timeframe, count)`** — timeframe enum M1|M5|M15|M30|H1|H4|D1|W1, count 1–500 (default 100);
closed candles only, NY time strings, oldest first.

**`validator_rules()`** — active profile, thresholds (§13 of rules), model windows, required fields. Short.

**`setup_submit(…)`** — parameters exactly as `validation-rules.md` §2. Returns
`{"status":"ARMED|REJECTED|DUPLICATE","setup_id":"XAU-0916-K7QD","reasons":[{"code":"G-10","text":"…sq…"}],"warnings":[…],"computed":{…},"replaced_setup_id":null}`.
Setup id format: 3-letter symbol prefix, NY `MMDD`, 4 random base32 chars.

**`setup_status(setup_id?)`** — with id: state, timeline (last 20 events), live price, distance to zone/SL/TPs.
Without: active setups + last 10 closed.

**`setup_cancel(setup_id)`** — ARMED/IN_ZONE → CANCELLED (Telegram line). TRIGGERED cannot be cancelled (tracking only).

Tool descriptions must tell Gemini: use only snapshot prices; `pda_formed_at_ny` copied from snapshot `formed_at`;
never retry a REJECTED setup with loosened levels.

## 7. Tests (required)

- OAuth e2e with an in-process HTTP client: metadata discovery → DCR → `/authorize` (PKCE S256) → login wrong password
  (lockout counters) → login ok → `/token` → `tools/list` on `/mcp` with bearer → refresh rotation (+ grace) → revoke.
- Restart persistence: build a new app instance on the same SQLite file; the previous access and refresh tokens still work.
- Host header: `Host: something.up.railway.app` → 401 without token, 200 with token (never 421).
- Unauthenticated `/mcp` → 401 with `WWW-Authenticate` containing `resource_metadata`.
- Missing `resource` in the authorization request → token still valid for `/mcp`.
- Schema lint over all tools; addendum-vs-tools test (tool names + section D parameter column of `docs/GEMINI_V11_ADDENDUM.md`).
- Lifespan runs once across 20 MCP requests.
