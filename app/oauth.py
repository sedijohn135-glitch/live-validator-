"""SQLite-backed OAuth 2.1 authorization server for the Gemini Spark link.

Everything persists, so the link survives a redeploy (as long as the volume does). Tokens are opaque
and only their SHA-256 hashes are stored.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from collections.abc import Callable
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from app.config import Settings
from app.store import Store

logger = logging.getLogger(__name__)

PENDING_TTL_S = 600
CODE_TTL_S = 300
ACCESS_TTL_S = 24 * 3600
REFRESH_TTL_S = 90 * 24 * 3600
REFRESH_GRACE_S = 600

IP_FAILURE_LIMIT = 5
IP_WINDOW_S = 15 * 60
IP_LOCK_S = 15 * 60
GLOBAL_FAILURE_LIMIT = 20
GLOBAL_WINDOW_S = 3600
GLOBAL_LOCK_S = 3600


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalise_resource(value: str | None) -> str | None:
    if not value:
        return None
    return value.rstrip("/")


class SQLiteOAuthProvider:
    """Implements `OAuthAuthorizationServerProvider` on top of the project's SQLite store."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        notify: Callable[[str, str], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.store = store
        self.clock = clock
        self._notify = notify or (lambda key, text: None)

    # ------------------------------------------------------------------ config
    @property
    def base_url(self) -> str:
        return self.settings.public_base_url.rstrip("/")

    @property
    def resource_url(self) -> str:
        return f"{self.base_url}/mcp"

    def _static_client(self) -> OAuthClientInformationFull | None:
        if not self.settings.oauth_static_client_id:
            return None
        return OAuthClientInformationFull(
            client_id=self.settings.oauth_static_client_id,
            client_secret=self.settings.oauth_static_client_secret or None,
            redirect_uris=list(self.settings.oauth_static_redirect_uris) or ["https://example.com/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="client_secret_post"
            if self.settings.oauth_static_client_secret
            else "none",
        )

    # ----------------------------------------------------------------- clients
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        static = self._static_client()
        if static is not None and static.client_id == client_id:
            return static
        row = self.store.query_one("SELECT data_json FROM oauth_clients WHERE client_id = ?", (client_id,))
        if row is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate_json(row["data_json"])
        except ValueError:  # pragma: no cover - corrupted row
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.redirect_uris:
            raise RegistrationError("invalid_redirect_uri", "at least one redirect_uri is required")
        self.store.execute(
            "INSERT INTO oauth_clients(client_id, data_json, created_at) VALUES(?,?,?) "
            "ON CONFLICT(client_id) DO UPDATE SET data_json = excluded.data_json",
            (client_info.client_id, client_info.model_dump_json(), self.clock()),
        )

    # --------------------------------------------------------------- authorize
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        requested = normalise_resource(params.resource)
        if requested is not None and requested != normalise_resource(self.resource_url):
            raise AuthorizeError("invalid_target", "unknown resource")
        tx = secrets.token_urlsafe(32)
        payload = {
            "client_id": client.client_id,
            "state": params.state,
            "scopes": params.scopes or [],
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": requested or normalise_resource(self.resource_url),
        }
        now = self.clock()
        self.store.execute(
            "INSERT INTO oauth_pending(tx, client_id, params_json, created_at, expires_at) VALUES(?,?,?,?,?)",
            (tx, client.client_id, json.dumps(payload), now, now + PENDING_TTL_S),
        )
        return f"{self.base_url}/oauth/login?tx={tx}"

    # -------------------------------------------------------------------- login
    def csrf_token(self, tx: str) -> str:
        return hmac.new(self.store.secret_key(), tx.encode(), hashlib.sha256).hexdigest()

    def pending(self, tx: str) -> dict[str, Any] | None:
        row = self.store.query_one("SELECT * FROM oauth_pending WHERE tx = ?", (tx,))
        if row is None or row["expires_at"] < self.clock():
            return None
        return json.loads(row["params_json"])

    def _attempts(self, scope: str) -> tuple[int, float | None, float | None]:
        row = self.store.query_one("SELECT * FROM login_attempts WHERE scope = ?", (scope,))
        if row is None:
            return 0, None, None
        return row["failures"], row["first_failure"], row["locked_until"]

    def locked_until(self, ip: str) -> float | None:
        """The later of the per-IP and global locks, if either is active."""
        now = self.clock()
        locks = []
        for scope in (f"ip:{ip}", "global"):
            _failures, _first, until = self._attempts(scope)
            if until and until > now:
                locks.append(until)
        return max(locks) if locks else None

    def record_failure(self, ip: str) -> None:
        now = self.clock()
        for scope, limit, window, lock in (
            (f"ip:{ip}", IP_FAILURE_LIMIT, IP_WINDOW_S, IP_LOCK_S),
            ("global", GLOBAL_FAILURE_LIMIT, GLOBAL_WINDOW_S, GLOBAL_LOCK_S),
        ):
            failures, first, _until = self._attempts(scope)
            if first is None or now - first > window:
                failures, first = 0, now
            failures += 1
            locked_until = now + lock if failures >= limit else None
            self.store.execute(
                "INSERT INTO login_attempts(scope, failures, first_failure, locked_until) VALUES(?,?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET failures = excluded.failures, "
                "first_failure = excluded.first_failure, locked_until = excluded.locked_until",
                (scope, failures, first, locked_until),
            )
            if locked_until and scope == "global":
                self._notify("login_attack", "LOGIN_ATTACK")

    def clear_failures(self, ip: str) -> None:
        self.store.execute("DELETE FROM login_attempts WHERE scope = ?", (f"ip:{ip}",))

    def check_password(self, password: str) -> bool:
        expected = self.settings.owner_password
        if not expected:
            return False
        return hmac.compare_digest(password.encode(), expected.encode())

    def issue_code(self, tx: str, payload: dict[str, Any]) -> str:
        code = secrets.token_urlsafe(48)
        now = self.clock()
        family = secrets.token_urlsafe(16)
        self.store.execute(
            "INSERT INTO oauth_codes(code_hash, client_id, data_json, expires_at, family) VALUES(?,?,?,?,?)",
            (sha256(code), payload["client_id"], json.dumps(payload), now + CODE_TTL_S, family),
        )
        self.store.execute("DELETE FROM oauth_pending WHERE tx = ?", (tx,))
        self._notify("gemini_linked", "GEMINI_LINKED")
        return code

    def redirect_target(self, payload: dict[str, Any], code: str) -> str:
        return construct_redirect_uri(payload["redirect_uri"], code=code, state=payload.get("state"))

    # ------------------------------------------------------------------- codes
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self.store.query_one("SELECT * FROM oauth_codes WHERE code_hash = ?", (sha256(authorization_code),))
        if row is None or row["client_id"] != client.client_id:
            return None
        if row["used_at"] is not None:
            # Replay: the whole token family is compromised.
            self._revoke_family(row["family"])
            return None
        if row["expires_at"] < self.clock():
            return None
        payload = json.loads(row["data_json"])
        return AuthorizationCode(
            code=authorization_code,
            scopes=payload.get("scopes") or [],
            expires_at=row["expires_at"],
            client_id=client.client_id,
            code_challenge=payload["code_challenge"],
            redirect_uri=payload["redirect_uri"],
            redirect_uri_provided_explicitly=payload["redirect_uri_provided_explicitly"],
            resource=payload.get("resource"),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        code_hash = sha256(authorization_code.code)
        row = self.store.query_one("SELECT * FROM oauth_codes WHERE code_hash = ?", (code_hash,))
        if row is None or row["used_at"] is not None:
            raise TokenError("invalid_grant", "authorization code already used")
        now = self.clock()
        self.store.execute("UPDATE oauth_codes SET used_at = ? WHERE code_hash = ?", (now, code_hash))
        return self._issue_pair(
            client.client_id,
            row["family"],
            authorization_code.scopes,
            authorization_code.resource or normalise_resource(self.resource_url),
        )

    # ---------------------------------------------------------------- refresh
    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = self._token_row(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=int(row["expires_at"]) if row["expires_at"] else None,
            resource=row["resource"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        row = self._token_row(refresh_token.token, "refresh")
        if row is None:
            raise TokenError("invalid_grant", "unknown refresh token")
        now = self.clock()
        # Rotation with a grace window: a retried request must not break the link.
        self.store.execute(
            "UPDATE oauth_tokens SET revoked_at = ?, grace_until = ? WHERE token_hash = ?",
            (now, now + REFRESH_GRACE_S, row["token_hash"]),
        )
        return self._issue_pair(
            client.client_id, row["family"], scopes or json.loads(row["scopes"]), row["resource"]
        )

    # ----------------------------------------------------------------- access
    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self._token_row(token, "access")
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=int(row["expires_at"]) if row["expires_at"] else None,
            resource=row["resource"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        row = self.store.query_one("SELECT family FROM oauth_tokens WHERE token_hash = ?", (sha256(token.token),))
        if row is not None:
            self._revoke_family(row["family"])

    def revoke_all(self) -> int:
        rows = self.store.query("SELECT token_hash FROM oauth_tokens WHERE revoked_at IS NULL")
        self.store.execute("UPDATE oauth_tokens SET revoked_at = ? WHERE revoked_at IS NULL", (self.clock(),))
        self.store.execute("DELETE FROM oauth_codes", ())
        return len(rows)

    def linked(self) -> bool:
        row = self.store.query_one(
            "SELECT COUNT(*) AS n FROM oauth_tokens WHERE kind = 'refresh' AND revoked_at IS NULL"
        )
        return bool(row and row["n"])

    # ---------------------------------------------------------------- helpers
    def _token_row(self, token: str, kind: str):
        row = self.store.query_one(
            "SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = ?", (sha256(token), kind)
        )
        if row is None:
            return None
        now = self.clock()
        if row["expires_at"] and row["expires_at"] < now:
            return None
        if row["revoked_at"] is not None:
            grace = row["grace_until"]
            if not (kind == "refresh" and grace and grace > now):
                return None
        return row

    def _issue_pair(self, client_id: str, family: str, scopes: list[str], resource: str | None) -> OAuthToken:
        now = self.clock()
        access = secrets.token_urlsafe(48)
        refresh = secrets.token_urlsafe(48)
        with self.store.transaction() as conn:
            for token, kind, ttl in ((access, "access", ACCESS_TTL_S), (refresh, "refresh", REFRESH_TTL_S)):
                conn.execute(
                    "INSERT INTO oauth_tokens(token_hash, kind, client_id, family, scopes, resource, "
                    "expires_at, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (sha256(token), kind, client_id, family, json.dumps(scopes), resource, now + ttl, now),
                )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL_S,
            refresh_token=refresh,
            scope=" ".join(scopes) if scopes else None,
        )

    def _revoke_family(self, family: str) -> None:
        self.store.execute(
            "UPDATE oauth_tokens SET revoked_at = ?, grace_until = NULL WHERE family = ?", (self.clock(), family)
        )


# ------------------------------------------------------------------ login page
LOGIN_CSS = """
:root { color-scheme: light dark; --bg:#0f1115; --fg:#e8eaed; --card:#171a21; --accent:#1f6feb; }
@media (prefers-color-scheme: light) { :root { --bg:#f6f7f9; --fg:#14161a; --card:#fff; } }
* { box-sizing: border-box; }
body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
  background:var(--bg); color:var(--fg); font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; padding:16px; }
.card { background:var(--card); border-radius:16px; padding:24px; width:100%; max-width:380px;
  box-shadow:0 8px 30px rgba(0,0,0,.18); }
h1 { font-size:20px; margin:0 0 4px; } p { margin:4px 0 16px; opacity:.75; font-size:14px; }
input { width:100%; padding:14px; font-size:16px; border-radius:10px; border:1px solid #8884;
  background:transparent; color:var(--fg); }
button { width:100%; margin-top:14px; padding:14px; font-size:16px; border:0; border-radius:10px;
  background:var(--accent); color:#fff; font-weight:600; }
.error { color:#ff6b6b; font-size:14px; margin-top:12px; }
"""


def login_page(tx: str, csrf: str, error: str = "", disabled: bool = False) -> str:
    """Mobile-friendly password page (Albanian, with English labels)."""
    body = (
        "<p>Fjalëkalimi i pronarit / Owner password</p>"
        '<input type="password" name="password" autocomplete="current-password" autofocus>'
        '<button type="submit">Lejo / Allow</button>'
    )
    if disabled:
        body = (
            "<p>OWNER_PASSWORD nuk është vendosur te Railway. "
            "Vendose te Variables dhe provo sërish.<br>"
            "OWNER_PASSWORD is not set; add it in Railway Variables.</p>"
        )
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    return (
        "<!doctype html><html lang='sq'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>live-validator</title>"
        f"<style>{LOGIN_CSS}</style></head><body><div class='card'>"
        "<h1>live-validator</h1>"
        '<form method="post" action="/oauth/login">'
        f'<input type="hidden" name="tx" value="{html.escape(tx)}">'
        f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
        f"{body}{error_html}"
        "</form></div></body></html>"
    )


def client_ip(headers: dict[str, str], fallback: str = "0.0.0.0") -> str:
    forwarded = headers.get("x-forwarded-for") or ""
    if forwarded:
        return forwarded.split(",")[0].strip()
    return headers.get("x-real-ip") or fallback
