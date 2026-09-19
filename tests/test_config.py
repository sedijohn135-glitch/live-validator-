from app.config import UNIVERSAL, load_settings, resolve_data_dir


def test_defaults_without_any_environment():
    settings = load_settings({})
    assert settings.profile is UNIVERSAL
    assert settings.symbols == ("XAUUSD", "BTCUSD")
    assert not settings.telegram_configured()
    assert not settings.ctrader_configured()
    assert settings.public_base_url == "http://localhost:8080"


def test_garbage_environment_never_raises():
    settings = load_settings(
        {
            "VALIDATOR_PROFILE": "banana",
            "PRICE_DIGITS": "{not json",
            "PRICE_BANDS": '{"XAUUSD": "nope"}',
            "MAX_SPREAD": '{"XAUUSD": "x"}',
            "SYMBOL_MAP": "[]",
            "OWNER_PASSWORD": "short",
        }
    )
    assert settings.profile is UNIVERSAL
    assert len(settings.warnings) >= 5
    assert settings.symbol("XAUUSD").display_decimals == 2


def test_the_profile_variable_is_no_longer_used():
    """One validator, one profile: a leftover VALIDATOR_PROFILE only earns a warning."""
    settings = load_settings({"VALIDATOR_PROFILE": "balanced"})
    assert settings.profile is UNIVERSAL
    assert any("VALIDATOR_PROFILE" in warning for warning in settings.warnings)


def test_symbol_overrides():
    settings = load_settings(
        {"PRICE_DIGITS": '{"XAUUSD": 3}', "MAX_SPREAD": '{"XAUUSD": 0.5}', "PRICE_BANDS": '{"XAUUSD": [100, 200]}'}
    )
    sym = settings.symbol("XAUUSD")
    assert sym.display_decimals == 3
    assert sym.max_spread_abs == 0.5
    assert sym.price_band == (100.0, 200.0)


def test_public_base_url_is_normalised():
    assert load_settings({"PUBLIC_BASE_URL": "https://x.up.railway.app/"}).public_base_url == "https://x.up.railway.app"
    assert load_settings({"RAILWAY_PUBLIC_DOMAIN": "x.up.railway.app"}).public_base_url == "https://x.up.railway.app"


def test_data_dir_resolution_and_volume_warning():
    path, on_volume, warning = resolve_data_dir({})
    assert path == "./data" and not on_volume and warning == ""

    path, on_volume, warning = resolve_data_dir({"RAILWAY_ENVIRONMENT": "production"})
    assert path == "./data" and not on_volume
    assert "Volume" in warning

    path, on_volume, warning = resolve_data_dir(
        {"RAILWAY_ENVIRONMENT": "production", "RAILWAY_VOLUME_MOUNT_PATH": "/data"}
    )
    assert path == "/data" and on_volume and warning == ""

    path, on_volume, _ = resolve_data_dir({"DATA_DIR": "/custom", "RAILWAY_VOLUME_MOUNT_PATH": "/data"})
    assert path == "/custom" and not on_volume


def test_the_app_name_is_configurable_and_bounded():
    """One Telegram, several bots: each one says which it is, and the owner picks the words."""
    assert load_settings({}).app_name == "Live Validator"
    assert load_settings({"APP_NAME": "  Sniper XAU  "}).app_name == "Sniper XAU"
    assert len(load_settings({"APP_NAME": "x" * 200}).app_name) == 40
