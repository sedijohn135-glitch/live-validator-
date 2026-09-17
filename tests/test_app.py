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
    assert payload["version"] and payload["profile"] == "STRICT"
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
