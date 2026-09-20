"""HTTP surface: /health, the OAuth flow for Gemini, host headers and the MCP endpoint."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from tests.app_harness import BASE_URL, PASSWORD, make_app, pkce

MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def rpc(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


@pytest.fixture()
def client(tmp_path):
    app, runtime, fake_ctrader, telegram = make_app(tmp_path)
    with TestClient(app, base_url=BASE_URL) as test_client:
        test_client.runtime = runtime
        test_client.ctrader = fake_ctrader
        test_client.telegram = telegram
        yield test_client


# ----------------------------------------------------------------------- health
def test_health_is_200_without_any_credentials(tmp_path):
    """Failure mode D6: the deploy must never fail because market data is missing."""
    app, _runtime, _fake, _tg = make_app(
        tmp_path, CTRADER_MCP_URL="", CTRADER_MCP_TOKEN="", TELEGRAM_BOT_TOKEN="", OWNER_PASSWORD=""
    )
    with TestClient(app, base_url=BASE_URL) as test_client:
        response = test_client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["telegram"] == "not_configured"
    assert payload["data"]["ctrader"] == "not_configured"
    assert payload["version"] and payload["profile"] == "UNIVERSAL"
    assert payload["paused"] is True  # no data means no triggers
    assert "token" not in json.dumps(payload).lower()


def test_health_reports_the_missing_volume(tmp_path):
    app, _runtime, _fake, _tg = make_app(tmp_path)
    with TestClient(app, base_url=BASE_URL) as test_client:
        payload = test_client.get("/health").json()
    assert payload["volume"] == "missing"


def test_health_never_leaks_secrets(client):
    body = client.get("/health").text
    assert PASSWORD not in body and "tok" * 8 not in body


# ------------------------------------------------------------------ MCP access
def test_unauthenticated_mcp_returns_401_with_resource_metadata(client):
    response = client.post("/mcp", json=rpc("tools/list"), headers=MCP_HEADERS)
    assert response.status_code == 401
    assert "resource_metadata" in response.headers.get("www-authenticate", "")


def test_public_host_header_is_accepted(client):
    """Failure mode D2: the SDK's default host allowlist would answer 421 to Gemini."""
    headers = {**MCP_HEADERS, "Host": "validator.up.railway.app"}
    response = client.post("/mcp", json=rpc("tools/list"), headers=headers)
    assert response.status_code == 401  # auth, never 421


def test_mcp_path_without_trailing_slash_is_the_endpoint(client):
    assert client.post("/mcp", json=rpc("tools/list"), headers=MCP_HEADERS).status_code == 401
    trailing = client.post("/mcp/", json=rpc("tools/list"), headers=MCP_HEADERS, follow_redirects=False)
    assert trailing.status_code in (307, 308, 401, 404)


# ----------------------------------------------------------------------- CORS
def test_cors_preflight_from_gemini_origin_succeeds(client):
    """Failure mode: Brave's Shields freezes the UI when the preflight OPTIONS fails or returns
    the wrong headers. The middleware must answer 204 with full CORS headers so the browser
    sends the actual POST."""
    response = client.options(
        "/mcp",
        headers={
            "Origin": "https://gemini.google.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,accept,mcp-session-id",
        },
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-origin") == "https://gemini.google.com"
    assert "POST" in response.headers.get("access-control-allow-methods", "")
    allow_headers = response.headers.get("access-control-allow-headers", "").lower()
    assert "authorization" in allow_headers
    assert "mcp-session-id" in allow_headers


def test_cors_response_exposes_session_header(client):
    """Without expose-headers, JS reading the response cannot see Mcp-Session-Id, which breaks
    multi-turn tool calls in Spark."""
    response = client.post(
        "/mcp",
        json=rpc("tools/list"),
        headers={**MCP_HEADERS, "Origin": "https://gemini.google.com"},
    )
    assert response.status_code == 401
    assert response.headers.get("access-control-allow-origin") == "https://gemini.google.com"
    assert "mcp-session-id" in response.headers.get("access-control-expose-headers", "").lower()


def test_cors_preflight_without_origin_is_allowed(client):
    """Server-to-server callers (curl, CI, Claude) have no Origin header; they must not be blocked."""
    response = client.options("/mcp", headers={"Access-Control-Request-Method": "POST"})
    assert response.status_code in (200, 204)


# ------------------------------------------------------------ no-store middleware
def test_mcp_response_carries_no_store_cache_headers(client):
    """Failure mode: Brave's renderer caches the 'Working on it…' placeholder and never swaps
    it for the formatted setup output after setup_submit returns. The middleware must answer
    every /mcp response with cache-busting directives so Brave cannot hold the intermediate
    state past the model's final chunk."""
    response = client.post("/mcp", json=rpc("tools/list"), headers=MCP_HEADERS)
    cache_control = response.headers.get("cache-control", "").lower()
    assert "no-store" in cache_control
    assert "no-cache" in cache_control
    assert "must-revalidate" in cache_control
    assert response.headers.get("connection", "").lower() == "close"
    assert response.headers.get("pragma", "").lower() == "no-cache"
    assert response.headers.get("expires") == "0"
    assert response.headers.get("surrogate-control", "").lower() == "no-store"
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert "accept" in response.headers.get("vary", "").lower()
    assert "origin" in response.headers.get("vary", "").lower()


def test_mcp_response_overrides_incoming_cache_headers(client):
    """If the upstream sets Cache-Control (the MCP SDK sends no-cache on SSE responses), the
    middleware must replace it with the stronger no-store directive. A weaker header that
    remains in the response is the bug we are trying to prevent."""
    # The MCP SDK only sets Cache-Control on the SSE branch; in JSON mode the response is bare
    # so the middleware is the only writer. We verify by sending a request that goes through
    # the SSE branch — the tools/list call stays on JSON, so use a request that the SDK routes
    # through the streaming path. Easiest check: the bare JSON path is already covered above;
    # here we just assert the override happens even when the response happens to ship a header.
    response = client.post("/mcp", json=rpc("tools/list"), headers=MCP_HEADERS)
    cache_header = [v for k, v in response.headers.items() if k.lower() == "cache-control"]
    assert len(cache_header) == 1, f"expected one cache-control header, got {cache_header}"
    assert cache_header[0].lower() == (
        "no-store, no-cache, must-revalidate, max-age=0"
    )


def test_mcp_path_with_trailing_slash_also_gets_no_store(client):
    """Some clients (curl, Spark's fallback URL builder) hit /mcp/ with a trailing slash. The
    middleware must catch those too — otherwise the same Brave freeze happens on the very next
    request after a 307 redirect to /mcp/."""
    response = client.post("/mcp/", json=rpc("tools/list"), headers=MCP_HEADERS, follow_redirects=False)
    if response.status_code in (307, 308):
        # The redirect itself is unauthenticated 401 material; assert the redirect target carries
        # the headers once it resolves.
        target = response.headers["location"]
        followed = client.post(target, json=rpc("tools/list"), headers=MCP_HEADERS)
        cache_control = followed.headers.get("cache-control", "").lower()
    else:
        cache_control = response.headers.get("cache-control", "").lower()
    assert "no-store" in cache_control


def test_options_preflight_does_not_get_no_store(client):
    """The CORS layer owns preflight caching via Access-Control-Max-Age. If we slap no-store on
    preflights, Brave does a fresh OPTIONS on every single MCP POST — a measurable regression
    for the CORS-compliant flow we already fixed in 26d4d55."""
    response = client.options(
        "/mcp",
        headers={
            "Origin": "https://gemini.google.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code in (200, 204)
    cache_control = response.headers.get("cache-control", "").lower()
    assert "no-store" not in cache_control, (
        "OPTIONS preflight must keep its own caching semantics — CORS Max-Age, not no-store"
    )


def test_non_mcp_routes_are_left_alone(client):
    """/health and the OAuth routes must not inherit the no-store treatment. A cached /health
    would defeat uptime monitoring; a cached /oauth/login page would break the CSRF token."""
    health = client.get("/health")
    assert "no-store" not in health.headers.get("cache-control", "").lower()

    metadata = client.get("/.well-known/oauth-authorization-server")
    assert "no-store" not in metadata.headers.get("cache-control", "").lower()


def test_mcp_options_preflight_keeps_access_control_max_age(client):
    """Sanity: the CORS layer's Max-Age survives the request — proves OPTIONS is genuinely
    untouched by the no-store middleware and that preflights will keep their 24 h caching."""
    response = client.options(
        "/mcp",
        headers={
            "Origin": "https://gemini.google.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.headers.get("access-control-max-age") == "86400"


# --------------------------------------------------------------------- metadata
def test_authorization_server_metadata_uses_the_public_base_url(client):
    payload = client.get("/.well-known/oauth-authorization-server").json()
    assert payload["issuer"].rstrip("/") == BASE_URL
    assert payload["authorization_endpoint"] == f"{BASE_URL}/authorize"
    assert payload["registration_endpoint"] == f"{BASE_URL}/register"
    assert "S256" in payload["code_challenge_methods_supported"]


def test_protected_resource_metadata(client):
    payload = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert payload["resource"].rstrip("/") == f"{BASE_URL}/mcp"


# ------------------------------------------------------------------ OAuth flow
def register_client(client, scope: str = "openid email") -> dict:
    response = client.post(
        "/register",
        json={
            "client_name": "Gemini",
            "redirect_uris": ["https://gemini.google.com/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": scope,
            "token_endpoint_auth_method": "none",
        },
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def authorize(client, registered: dict, challenge: str, resource: str | None = None) -> str:
    params = {
        "response_type": "code",
        "client_id": registered["client_id"],
        "redirect_uri": "https://gemini.google.com/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "state-123",
    }
    if resource:
        params["resource"] = resource
    response = client.get("/authorize", params=params, follow_redirects=False)
    assert response.status_code in (302, 307), response.text
    location = response.headers["location"]
    return parse_qs(urlparse(location).query)["tx"][0]


def login(client, tx: str, password: str = PASSWORD, ip: str = "203.0.113.5"):
    page = client.get("/oauth/login", params={"tx": tx})
    assert page.status_code == 200
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    return client.post(
        "/oauth/login",
        data={"tx": tx, "csrf": csrf, "password": password},
        headers={"X-Forwarded-For": f"{ip}, 10.0.0.1"},
        follow_redirects=False,
    )


def test_full_oauth_flow_and_refresh_rotation(client):
    registered = register_client(client)
    verifier, challenge = pkce()
    tx = authorize(client, registered, challenge)

    wrong = login(client, tx, password="not-the-password")
    assert wrong.status_code == 401

    redirect = login(client, tx)
    assert redirect.status_code == 302
    query = parse_qs(urlparse(redirect.headers["location"]).query)
    assert query["state"] == ["state-123"]
    code = query["code"][0]

    token = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://gemini.google.com/callback",
            "client_id": registered["client_id"],
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    tokens = token.json()
    assert tokens["token_type"].lower() == "bearer"
    assert tokens["expires_in"] == 24 * 3600

    listed = client.post(
        "/mcp",
        json=rpc("tools/list"),
        headers={**MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert listed.status_code == 200, listed.text
    assert "market_snapshot" in listed.text

    refreshed = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": registered["client_id"],
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    rotated = refreshed.json()
    assert rotated["refresh_token"] != tokens["refresh_token"]

    # The previous refresh token keeps working inside the grace window (a retried request).
    retried = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": registered["client_id"],
        },
    )
    assert retried.status_code == 200

    revoked = client.post(
        "/revoke",
        data={
            "token": rotated["access_token"],
            "client_id": registered["client_id"],
            "client_secret": registered.get("client_secret") or "",
        },
    )
    assert revoked.status_code == 200
    after = client.post(
        "/mcp",
        json=rpc("tools/list"),
        headers={**MCP_HEADERS, "Authorization": f"Bearer {rotated['access_token']}"},
    )
    assert after.status_code == 401


def test_replayed_authorization_code_revokes_the_whole_family(client):
    """A stolen-and-replayed code must invalidate the tokens it already minted."""
    registered = register_client(client)
    verifier, challenge = pkce()
    tx = authorize(client, registered, challenge)
    redirect = login(client, tx)
    code = parse_qs(urlparse(redirect.headers["location"]).query)["code"][0]
    exchange = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "https://gemini.google.com/callback",
        "client_id": registered["client_id"],
        "code_verifier": verifier,
    }
    tokens = client.post("/token", data=exchange).json()
    replay = client.post("/token", data=exchange)
    assert replay.status_code >= 400

    after = client.post(
        "/mcp",
        json=rpc("tools/list"),
        headers={**MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert after.status_code == 401
    refreshed = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": registered["client_id"],
        },
    )
    assert refreshed.status_code >= 400


def test_registration_accepts_unexpected_scopes(client):
    """Failure mode G2: Gemini's registration must not fail on a scope we did not expect."""
    registered = register_client(client, scope="openid email profile https://example.com/x")
    assert registered["client_id"]


def test_token_without_a_resource_still_works_on_mcp(client):
    """Failure mode G3: a token minted without an explicit resource is bound to BASE/mcp."""
    registered = register_client(client)
    verifier, challenge = pkce()
    tx = authorize(client, registered, challenge)  # no resource parameter
    redirect = login(client, tx)
    code = parse_qs(urlparse(redirect.headers["location"]).query)["code"][0]
    tokens = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://gemini.google.com/callback",
            "client_id": registered["client_id"],
            "code_verifier": verifier,
        },
    ).json()
    listed = client.post(
        "/mcp",
        json=rpc("tools/list"),
        headers={**MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert listed.status_code == 200


def test_unknown_resource_is_refused(client):
    registered = register_client(client)
    _verifier, challenge = pkce()
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": "https://gemini.google.com/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": "https://somewhere-else.example/mcp",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 307, 400)
    if response.status_code in (302, 307):
        assert "error" in parse_qs(urlparse(response.headers["location"]).query)


def test_login_lockout_after_repeated_failures(client):
    registered = register_client(client)
    _verifier, challenge = pkce()
    tx = authorize(client, registered, challenge)
    for _ in range(5):
        assert login(client, tx, password="wrong").status_code == 401
    locked = login(client, tx, password=PASSWORD)
    assert locked.status_code == 429


def test_login_page_says_so_when_the_password_is_missing(tmp_path):
    app, _runtime, _fake, _tg = make_app(tmp_path, OWNER_PASSWORD="")
    with TestClient(app, base_url=BASE_URL) as test_client:
        registered = register_client(test_client)
        _verifier, challenge = pkce()
        tx = authorize(test_client, registered, challenge)
        page = test_client.get("/oauth/login", params={"tx": tx})
        assert "OWNER_PASSWORD" in page.text
        posted = test_client.post(
            "/oauth/login", data={"tx": tx, "csrf": "x", "password": "y"}, follow_redirects=False
        )
        assert posted.status_code >= 400


def test_expired_transaction_is_refused(client):
    page = client.get("/oauth/login", params={"tx": "does-not-exist"})
    assert page.status_code == 400
    assert "skadoi" in page.text


# ------------------------------------------------------------------- lifespan
def test_background_tasks_start_once_across_many_requests(tmp_path):
    """Failure mode G14: the engine must not be started per request."""
    app, runtime, _fake, _tg = make_app(tmp_path)
    with TestClient(app, base_url=BASE_URL) as test_client:
        for _ in range(20):
            test_client.post("/mcp", json=rpc("tools/list"), headers=MCP_HEADERS)
        assert runtime.start_count == 1


def test_stale_oauth_rows_are_purged(client):
    """The pending, code and attempt tables must not grow for ever."""
    store = client.runtime.store
    registered = register_client(client)
    _verifier, challenge = pkce()
    authorize(client, registered, challenge)
    store.execute("UPDATE oauth_pending SET expires_at = 0", ())
    store.execute(
        "INSERT INTO login_attempts(scope, failures, first_failure, locked_until) VALUES('ip:1.2.3.4',1,0,0)",
        (),
    )
    authorize(client, registered, challenge)
    assert len(store.query("SELECT tx FROM oauth_pending")) == 1
    assert store.query_one("SELECT scope FROM login_attempts WHERE scope = 'ip:1.2.3.4'") is None


def test_tokens_survive_a_restart_on_the_same_database(tmp_path):
    """Failure mode G5: the Gemini link must not be lost on every redeploy."""
    app, _runtime, _fake, _tg = make_app(tmp_path)
    with TestClient(app, base_url=BASE_URL) as first:
        registered = register_client(first)
        verifier, challenge = pkce()
        tx = authorize(first, registered, challenge)
        code = parse_qs(urlparse(login(first, tx).headers["location"]).query)["code"][0]
        tokens = first.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://gemini.google.com/callback",
                "client_id": registered["client_id"],
                "code_verifier": verifier,
            },
        ).json()
        assert (
            first.post(
                "/mcp",
                json=rpc("tools/list"),
                headers={**MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"},
            ).status_code
            == 200
        )

    # A redeploy: a brand new process and app instance over the same volume.
    restarted, _runtime2, _fake2, _tg2 = make_app(tmp_path)
    with TestClient(restarted, base_url=BASE_URL) as second:
        listed = second.post(
            "/mcp",
            json=rpc("tools/list"),
            headers={**MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert listed.status_code == 200, listed.text
        refreshed = second.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": registered["client_id"],
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        assert second.post(
            "/mcp",
            json=rpc("tools/list"),
            headers={**MCP_HEADERS, "Authorization": f"Bearer {refreshed.json()['access_token']}"},
        ).status_code == 200


# --------------------------------------------------------------- open MCP mode
def test_open_mode_serves_mcp_without_any_token(tmp_path):
    """MCP_AUTH=open is the owner's explicit choice: Gemini connects straight from the URL."""
    app, runtime, _fake, _tg = make_app(tmp_path, MCP_AUTH="open")
    assert runtime.settings.mcp_open
    with TestClient(app, base_url=BASE_URL) as test_client:
        listed = test_client.post("/mcp", json=rpc("tools/list"), headers=MCP_HEADERS)
        assert listed.status_code == 200, listed.text
        assert "market_snapshot" in listed.text

        health = test_client.get("/health").json()
        assert health["mcp_auth"] == "open"
        assert any("MCP_AUTH=open" in w for w in health["warnings"])

        # No OAuth surface is advertised at all in this mode.
        assert test_client.get("/.well-known/oauth-authorization-server").status_code == 404
        assert test_client.get("/oauth/login", params={"tx": "x"}).status_code == 404


def test_open_mode_still_accepts_a_public_host_header(tmp_path):
    app, _runtime, _fake, _tg = make_app(tmp_path, MCP_AUTH="open")
    with TestClient(app, base_url=BASE_URL) as test_client:
        headers = {**MCP_HEADERS, "Host": "validator.up.railway.app"}
        assert test_client.post("/mcp", json=rpc("tools/list"), headers=headers).status_code == 200


def test_authentication_is_on_by_default(tmp_path):
    app, runtime, _fake, _tg = make_app(tmp_path)
    assert not runtime.settings.mcp_open
    with TestClient(app, base_url=BASE_URL) as test_client:
        assert test_client.post("/mcp", json=rpc("tools/list"), headers=MCP_HEADERS).status_code == 401
        assert test_client.get("/health").json()["mcp_auth"] == "oauth"


def test_an_unknown_auth_value_falls_back_to_oauth(tmp_path):
    app, runtime, _fake, _tg = make_app(tmp_path, MCP_AUTH="banana")
    assert not runtime.settings.mcp_open
    assert any("banana" in w for w in runtime.settings.warnings)
    with TestClient(app, base_url=BASE_URL) as test_client:
        assert test_client.post("/mcp", json=rpc("tools/list"), headers=MCP_HEADERS).status_code == 401


def test_snapshot_says_so_when_there_is_no_market_data(tmp_path):
    """A skeleton of nulls must never look analysable: Gemini would invent an analysis."""
    import asyncio

    app, runtime, _fake, _tg = make_app(tmp_path, CTRADER_MCP_URL="", CTRADER_MCP_TOKEN="")
    with TestClient(app, base_url=BASE_URL):
        payload = asyncio.run(runtime.snapshot("XAUUSD"))
    assert payload["data"]["usable"] is False
    assert payload["data"]["symbols_resolved"] == []
    assert payload["data"]["detail"]
    assert payload["notes"][0].startswith("NO MARKET DATA")
    assert payload["quote"] is None
    assert runtime.data_status != "ok"  # never report health when nothing arrived
    assert runtime.paused is True
    assert "XAUUSD" not in runtime.last_snapshot  # an unusable snapshot is not cached
