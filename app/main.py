"""Builds the MCP server, its OAuth routes, `/health` and the background lifespan.

The streamable-HTTP app is the root ASGI app: mounting it inside another Starlette app would stop
its lifespan (and therefore the engine) from ever running.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator

from mcp.server.auth.provider import construct_redirect_uri
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import tools as tools_module
from app.config import VERSION, load_settings
from app.oauth import SQLiteOAuthProvider, client_ip, login_page
from app.runtime import Runtime, SecretFilter
from app.store import Store

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Read-only IC Markets cTrader data plus a live ICT v11 setup validator. "
    "Prices and times come only from market_snapshot; the validator, not the model, decides when to "
    "enter, and the owner receives ENTER / DO NOT ENTER on Telegram."
)


def configure_logging(runtime: Runtime) -> None:
    level = getattr(logging, runtime.settings.log_level, logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    def secrets_provider() -> list[str]:
        settings = runtime.settings
        credentials = runtime.ctrader.credentials
        return [
            settings.telegram_bot_token,
            settings.owner_password,
            settings.ctrader_token,
            settings.oauth_static_client_secret,
            credentials.token if credentials else "",
        ]

    secret_filter = SecretFilter(secrets_provider)
    for handler in logging.getLogger().handlers:
        handler.addFilter(secret_filter)


def build_app(runtime: Runtime | None = None):
    """Create the ASGI app. Never raises on a bad environment: problems surface in `/health`."""
    runtime = runtime or Runtime()
    settings = runtime.settings
    provider = SQLiteOAuthProvider(settings, runtime.store, notify=runtime.notify, clock=runtime.clock)

    @contextlib.asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        configure_logging(runtime)
        await runtime.run_forever()
        try:
            yield
        finally:
            await runtime.stop()

    base = settings.public_base_url.rstrip("/")
    if settings.mcp_open:
        # MCP_AUTH=open: the owner chose to serve /mcp with no authentication at all, the way a
        # plain MCP server behaves. Gemini then connects straight from the URL, with no login page
        # and no client id or secret — and so can anyone else who knows the address.
        logger.warning("MCP_AUTH=open: /mcp is served without authentication")
        server = MCPServer(
            name="live-validator",
            instructions=INSTRUCTIONS,
            version=VERSION,
            lifespan=lifespan,
        )
    else:
        server = MCPServer(
            name="live-validator",
            instructions=INSTRUCTIONS,
            version=VERSION,
            auth_server_provider=provider,
            auth=AuthSettings(
                issuer_url=base,
                resource_server_url=f"{base}/mcp",
                client_registration_options=ClientRegistrationOptions(
                    enabled=True, valid_scopes=None, default_scopes=["mcp"]
                ),
                revocation_options=RevocationOptions(enabled=True),
                required_scopes=None,
                validate_token_resource=True,
            ),
            lifespan=lifespan,
        )

    tools_module.register(server, runtime)
    _register_routes(server, runtime, provider, login_routes=not settings.mcp_open)

    local = base.startswith("http://localhost") or base.startswith("http://127.0.0.1")
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False) if not local else None
    app = server.streamable_http_app(json_response=True, stateless_http=True, transport_security=security)
    app.state.runtime = runtime
    app.state.server = server
    app.state.provider = provider
    return app


def _register_routes(
    server: MCPServer, runtime: Runtime, provider: SQLiteOAuthProvider, login_routes: bool = True
) -> None:
    @server.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        """Always 200 while the process runs: a missing cTrader token must not fail the deploy."""
        try:
            payload = runtime.health()
        except Exception as exc:  # noqa: BLE001 - health must never throw
            payload = {"ok": True, "version": VERSION, "error": str(exc)[:200]}
        return JSONResponse(payload)

    if not login_routes:
        _ = health
        return

    @server.custom_route("/oauth/login", methods=["GET"])
    async def login_form(request: Request) -> HTMLResponse:
        tx = request.query_params.get("tx", "")
        pending = provider.pending(tx)
        if pending is None:
            return HTMLResponse(login_page("", "", "Lidhja skadoi. Provo sërish nga Gemini."), status_code=400)
        disabled = not runtime.settings.owner_password
        return HTMLResponse(login_page(tx, provider.csrf_token(tx), disabled=disabled))

    @server.custom_route("/oauth/login", methods=["POST"])
    async def login_submit(request: Request):
        form = await request.form()
        tx = str(form.get("tx") or "")
        csrf = str(form.get("csrf") or "")
        password = str(form.get("password") or "")
        fallback = request.client.host if request.client else "0.0.0.0"
        ip = client_ip({k.lower(): v for k, v in request.headers.items()}, fallback)

        pending = provider.pending(tx)
        if pending is None or not _constant_equal(csrf, provider.csrf_token(tx)):
            return HTMLResponse(login_page("", "", "Lidhja skadoi. Provo sërish nga Gemini."), status_code=400)
        if not runtime.settings.owner_password:
            return HTMLResponse(login_page(tx, provider.csrf_token(tx), disabled=True), status_code=503)
        locked = provider.locked_until(ip)
        if locked:
            return HTMLResponse(
                login_page(tx, provider.csrf_token(tx), "Shumë tentativa. Provo më vonë."), status_code=429
            )
        if not provider.check_password(password):
            provider.record_failure(ip)
            return HTMLResponse(
                login_page(tx, provider.csrf_token(tx), "Fjalëkalim i gabuar."), status_code=401
            )
        provider.clear_failures(ip)
        code = provider.issue_code(tx, pending)
        return RedirectResponse(provider.redirect_target(pending, code), status_code=302)

    _ = (health, login_form, login_submit, construct_redirect_uri)


def _constant_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode(), right.encode())


def _bootstrap() -> Runtime:
    settings = load_settings()
    os.makedirs(settings.data_dir, exist_ok=True)
    return Runtime(settings, Store(settings.db_path))


app = build_app(_bootstrap()) if os.environ.get("LIVE_VALIDATOR_NO_APP") != "1" else None
