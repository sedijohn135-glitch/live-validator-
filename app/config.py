"""Environment parsing, validator profiles and per-symbol settings.

Nothing in this module may raise at import time: a bad environment value must surface in
`/health` and Telegram, never as a crash loop (failure mode D12).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "1.0.0"

TIMEFRAMES: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
    "W1": 604800,
}

ENTRY_MODELS = (
    "ICT_2022",
    "MARKET_ANCHOR",
    "MODEL_2",
    "TURTLE_SOUP",
    "TURTLE_SOUP_DEFERRED",
    "SILVER_BULLET",
    "OTE",
    "IOFED",
    "LOW_RESISTANCE_RUN",
    "SM_THREE_STAGE",
    "VENOM",
    "OR_FIRST_FVG",
    "OR_PM_FIRST_FVG",
    "LUNCH_MACRO_PM",
)

PDA_TYPES = (
    "FVG",
    "INVERSION_FVG",
    "ORDER_BLOCK",
    "BREAKER_BLOCK",
    "MITIGATION_BLOCK",
    "REJECTION_BLOCK",
    "LIQUIDITY_VOID",
    "SUSPENSION_BLOCK",
    "BPR",
    "OLD_HIGH_LOW",
    "OTE_ZONE",
)

FVG_FAMILY = ("FVG", "LIQUIDITY_VOID", "BPR", "INVERSION_FVG")

DEFAULT_SYMBOLS = ("XAUUSD", "BTCUSD")


@dataclass(frozen=True)
class SymbolSettings:
    """Per-symbol constants (validation-rules §13, last paragraph)."""

    name: str
    max_spread_abs: float
    sb_min_dol: float
    display_decimals: int
    price_band: tuple[float, float]

    @property
    def tick(self) -> float:
        return 10.0**-self.display_decimals


SYMBOL_DEFAULTS: dict[str, SymbolSettings] = {
    "XAUUSD": SymbolSettings("XAUUSD", 0.80, 10.0, 2, (1500.0, 14000.0)),
    "BTCUSD": SymbolSettings("BTCUSD", 60.0, 300.0, 2, (25000.0, 240000.0)),
}


@dataclass(frozen=True)
class Profile:
    """Thresholds of validation-rules §13. Both profiles are tested."""

    name: str
    rr_min_plan: float
    rr_min_trigger: float
    checklist_min_pos: int
    checklist_max_neg: int
    score_min: int
    ce_hard_fvg: bool
    sl_min_atr_ltf: float
    sl_max_atr_h1: float
    spread_sl_mult: float
    chase_max_risk_frac: float
    cisd_lookback: int
    mss_lookback: int
    confirm_max_bars: int
    disp_body_atr: float
    disp_body_range: float
    level_tol_atr: float
    price_drift_atr_h1: float
    price_drift_pct: float
    level_range_atr_d1: float
    max_setup_lifetime_h: float
    liq_window_h: float
    spread_spike_mult: float
    news_before_min: int
    news_after_min: int
    ath_prox_atr_d1: float
    ath_model_prox_atr: float
    strict_pda_verify: bool
    friday_cutoff_ny: str
    enter_valid_min: int
    late_trigger_max_s: int
    outcome_horizon_h: float
    quote_poll_s: float = 2.0
    quote_max_age_s: float = 5.0
    intake_quote_max_age_s: float = 180.0  # arming tolerates a stand-in price; T-07 never does
    close_grace_s: float = 2.0
    data_outage_alert_s: float = 60.0
    dedup_window_min: float = 15.0
    model_2_lifetime_h: float = 24.0


STRICT = Profile(
    name="STRICT",
    rr_min_plan=2.0,
    rr_min_trigger=2.0,
    checklist_min_pos=7,
    checklist_max_neg=2,
    score_min=3,
    ce_hard_fvg=True,
    sl_min_atr_ltf=0.5,
    sl_max_atr_h1=3.0,
    spread_sl_mult=3.0,
    chase_max_risk_frac=0.35,
    cisd_lookback=10,
    mss_lookback=20,
    confirm_max_bars=12,
    disp_body_atr=0.8,
    disp_body_range=0.55,
    level_tol_atr=0.15,
    price_drift_atr_h1=2.0,
    price_drift_pct=0.25,
    level_range_atr_d1=6.0,
    max_setup_lifetime_h=8.0,
    liq_window_h=24.0,
    spread_spike_mult=2.5,
    news_before_min=15,
    news_after_min=15,
    ath_prox_atr_d1=1.0,
    ath_model_prox_atr=1.5,
    strict_pda_verify=False,
    friday_cutoff_ny="15:30",
    enter_valid_min=5,
    late_trigger_max_s=90,
    outcome_horizon_h=24.0,
)

BALANCED = replace(
    STRICT,
    name="BALANCED",
    rr_min_trigger=1.5,
    checklist_min_pos=6,
    score_min=2,
    ce_hard_fvg=False,
    sl_min_atr_ltf=0.4,
    sl_max_atr_h1=4.0,
    spread_sl_mult=2.0,
    chase_max_risk_frac=0.5,
    confirm_max_bars=20,
    disp_body_atr=0.6,
    disp_body_range=0.5,
    level_tol_atr=0.25,
    price_drift_atr_h1=3.0,
    price_drift_pct=0.4,
    level_range_atr_d1=8.0,
    max_setup_lifetime_h=12.0,
    liq_window_h=36.0,
    spread_spike_mult=3.0,
    news_before_min=10,
    news_after_min=10,
    ath_prox_atr_d1=0.5,
    ath_model_prox_atr=2.0,
    friday_cutoff_ny="16:00",
    late_trigger_max_s=120,
)

PROFILES = {"STRICT": STRICT, "BALANCED": BALANCED}


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _env_json(name: str, warnings: list[str]) -> dict[str, Any]:
    raw = _env(name)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        warnings.append(f"{name} nuk është JSON i vlefshëm — u injorua")
        return {}
    if not isinstance(value, dict):
        warnings.append(f"{name} duhet të jetë objekt JSON — u injorua")
        return {}
    return value


def _normalise_base_url(raw: str) -> str:
    url = raw.strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def resolve_data_dir(environ: dict[str, str] | None = None) -> tuple[str, bool, str]:
    """Return `(path, on_volume, warning)` (architecture §6, failure mode D4)."""
    env = os.environ if environ is None else environ
    explicit = (env.get("DATA_DIR") or "").strip()
    volume = (env.get("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    on_railway = bool((env.get("RAILWAY_ENVIRONMENT") or "").strip())
    if explicit:
        path = explicit
    elif volume:
        path = volume
    else:
        path = "./data"
    on_volume = bool(volume) and (not explicit or explicit.rstrip("/") == volume.rstrip("/"))
    warning = ""
    if on_railway and not on_volume:
        warning = (
            "S'ka Volume në Railway: lidhja me Gemini dhe setup-et humbin në çdo deploy. Shto Volume te /data."
        )
    return path, on_volume, warning


@dataclass
class Settings:
    """Parsed environment. Never raises; problems land in `warnings`."""

    profile: Profile = STRICT
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    symbol_settings: dict[str, SymbolSettings] = field(default_factory=dict)
    symbol_map: dict[str, str] = field(default_factory=dict)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    owner_password: str = ""
    ctrader_config: str = ""
    ctrader_url: str = ""
    ctrader_token: str = ""
    public_base_url: str = ""
    data_dir: str = "./data"
    on_volume: bool = False
    news_filter: bool = True
    news_feed_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    btc_weekend_entries: bool = False
    oauth_static_client_id: str = ""
    oauth_static_client_secret: str = ""
    oauth_static_redirect_uris: tuple[str, ...] = ()
    log_level: str = "INFO"
    mcp_auth: str = "oauth"
    warnings: list[str] = field(default_factory=list)
    dev_mode: bool = False

    @property
    def mcp_open(self) -> bool:
        """True when `/mcp` is served without any authentication (owner's explicit choice)."""
        return self.mcp_auth == "open"

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "validator.db")

    def symbol(self, name: str) -> SymbolSettings:
        return self.symbol_settings.get(name.upper()) or SYMBOL_DEFAULTS.get(
            name.upper(), SymbolSettings(name.upper(), 1.0, 10.0, 2, (0.01, 10_000_000.0))
        )

    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def ctrader_configured(self) -> bool:
        return bool(self.ctrader_config or (self.ctrader_url and self.ctrader_token))


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    """Parse the environment. Any unusable value is replaced by its default plus a warning."""
    env = dict(os.environ if environ is None else environ)
    prev = dict(os.environ)
    if environ is not None:  # keep _env helpers reading the injected mapping
        os.environ.clear()
        os.environ.update(environ)
    try:
        warnings: list[str] = []

        profile_name = _env("VALIDATOR_PROFILE", "STRICT").upper()
        profile = PROFILES.get(profile_name)
        if profile is None:
            warnings.append(f"VALIDATOR_PROFILE '{profile_name}' nuk njihet — po përdoret STRICT")
            profile = STRICT

        raw_symbols = _env("SYMBOLS")
        symbols = tuple(s.strip().upper() for s in raw_symbols.split(",") if s.strip()) or DEFAULT_SYMBOLS

        digits_override = _env_json("PRICE_DIGITS", warnings)
        bands_override = _env_json("PRICE_BANDS", warnings)
        spread_override = _env_json("MAX_SPREAD", warnings)
        symbol_settings: dict[str, SymbolSettings] = {}
        for name in symbols:
            base = SYMBOL_DEFAULTS.get(name, SymbolSettings(name, 1.0, 10.0, 2, (0.01, 10_000_000.0)))
            decimals = base.display_decimals
            if name in digits_override:
                try:
                    decimals = int(digits_override[name])
                except (TypeError, ValueError):
                    warnings.append(f"PRICE_DIGITS[{name}] i pavlefshëm — u injorua")
            band = base.price_band
            if name in bands_override:
                try:
                    low, high = (float(x) for x in bands_override[name])
                    band = (low, high) if low < high else band
                except (TypeError, ValueError):
                    warnings.append(f"PRICE_BANDS[{name}] i pavlefshëm — u injorua")
            max_spread = base.max_spread_abs
            if name in spread_override:
                try:
                    max_spread = float(spread_override[name])
                except (TypeError, ValueError):
                    warnings.append(f"MAX_SPREAD[{name}] i pavlefshëm — u injorua")
            symbol_settings[name] = SymbolSettings(name, max_spread, base.sb_min_dol, decimals, band)

        symbol_map_raw = _env_json("SYMBOL_MAP", warnings)
        symbol_map = {str(k).upper(): str(v) for k, v in symbol_map_raw.items()}

        base_url = _normalise_base_url(_env("PUBLIC_BASE_URL"))
        if not base_url:
            domain = _env("RAILWAY_PUBLIC_DOMAIN")
            if domain:
                base_url = _normalise_base_url(domain)
        if not base_url:
            base_url = "http://localhost:8080"

        data_dir, on_volume, volume_warning = resolve_data_dir(env)
        if volume_warning:
            warnings.append(volume_warning)

        password = _env("OWNER_PASSWORD")
        if password and len(password) < 12:
            warnings.append("OWNER_PASSWORD është më i shkurtër se 12 shenja")

        redirect_uris = tuple(u.strip() for u in _env("OAUTH_STATIC_REDIRECT_URIS").split(",") if u.strip())

        raw_auth = _env("MCP_AUTH", "oauth").lower()
        if raw_auth in ("open", "none", "off", "false", "0"):
            mcp_auth = "open"
            warnings.append(
                "MCP_AUTH=open: /mcp është pa fjalëkalim — kushdo me adresën mund të dërgojë setup"
            )
        elif raw_auth in ("oauth", "on", "true", "1", ""):
            mcp_auth = "oauth"
        else:
            mcp_auth = "oauth"
            warnings.append(f"MCP_AUTH '{raw_auth}' nuk njihet — po përdoret oauth")

        return Settings(
            profile=profile,
            symbols=symbols,
            symbol_settings=symbol_settings,
            symbol_map=symbol_map,
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
            owner_password=password,
            ctrader_config=_env("CTRADER_MCP_CONFIG"),
            ctrader_url=_env("CTRADER_MCP_URL"),
            ctrader_token=_env("CTRADER_MCP_TOKEN"),
            public_base_url=base_url,
            data_dir=data_dir,
            on_volume=on_volume,
            news_filter=_env_bool("NEWS_FILTER", True),
            news_feed_url=_env("NEWS_FEED_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.json"),
            btc_weekend_entries=_env_bool("BTC_WEEKEND_ENTRIES", False),
            oauth_static_client_id=_env("OAUTH_STATIC_CLIENT_ID"),
            oauth_static_client_secret=_env("OAUTH_STATIC_CLIENT_SECRET"),
            oauth_static_redirect_uris=redirect_uris,
            log_level=_env("LOG_LEVEL", "INFO").upper() or "INFO",
            mcp_auth=mcp_auth,
            warnings=warnings,
            dev_mode=_env_bool("DEV_MODE", False),
        )
    finally:
        if environ is not None:
            os.environ.clear()
            os.environ.update(prev)
