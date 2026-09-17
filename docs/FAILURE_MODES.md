# Failure modes — ticked before shipping

Every row of `.claude/skills/live-validator-builder/references/failure-modes.md`, with the mitigation
that is actually in the code and the test that proves it.

## Deployment / platform

| # | Mitigation in the code | Proof |
|---|---|---|
| D1 | `Dockerfile` CMD is shell form and binds `0.0.0.0:${PORT:-8080}` | `test_failure_modes.py::test_server_binds_the_platform_port_and_answers_health`, `test_docs.py::test_dockerfile_binds_the_platform_port_and_runs_as_root` |
| D2 | `main.py` passes `TransportSecuritySettings(enable_dns_rebinding_protection=False)` for any non-localhost base URL | `test_app.py::test_public_host_header_is_accepted` (401, never 421) |
| D3 | No `railway.json` / `railway.toml`; settings documented in `SETUP_SQ.md` | `test_docs.py::test_no_railway_config_as_code` |
| D4 | `config.resolve_data_dir` prefers `DATA_DIR` → `RAILWAY_VOLUME_MOUNT_PATH` → `./data` and warns loudly | `test_config.py::test_data_dir_resolution_and_volume_warning`, `test_app.py::test_health_reports_the_missing_volume` |
| D5 | The image never switches user | `test_docs.py::test_dockerfile_binds_the_platform_port_and_runs_as_root` |
| D6 | `/health` catches everything and always answers 200 | `test_app.py::test_health_is_200_without_any_credentials` |
| D7 | `tzdata` is a runtime dependency | `test_failure_modes.py::test_new_york_timezone_is_available` |
| D8 | `lease` row with a 30 s TTL; the engine, sender and command poller all run only under it | `test_store.py::test_lease_blocks_a_second_holder`, `test_failure_modes.py::test_a_second_instance_without_the_lease_sends_nothing` |
| D9 | `SETUP_SQ.md` step 1 turns serverless off; `/status` shows uptime | `test_docs.py::test_setup_doc_covers_the_required_variables` |
| D10 | `uv.lock` with exact pins; CI runs `uv sync --frozen` | `test_failure_modes.py::test_ci_uses_a_frozen_lockfile` |
| D11 | The work ends on `main`; see the final owner message | this document plus the repository history |
| D12 | `load_settings` never raises; every bad value becomes a warning | `test_config.py::test_garbage_environment_never_raises`, `test_failure_modes.py::test_a_broken_environment_never_crashes_the_process` |

## Gemini / MCP / OAuth

| # | Mitigation in the code | Proof |
|---|---|---|
| G1 | Every tool takes flat primitives; optional values use sentinel defaults | `test_tools.py::test_every_tool_schema_is_flat` |
| G2 | `valid_scopes=None`, `required_scopes=None` | `test_app.py::test_registration_accepts_unexpected_scopes` |
| G3 | The provider binds `BASE/mcp` when the client sends no `resource` | `test_app.py::test_token_without_a_resource_still_works_on_mcp` |
| G4 | `PUBLIC_BASE_URL` is normalised once; OAuth URLs are never built from the request | `test_config.py::test_public_base_url_is_normalised`, `test_app.py::test_authorization_server_metadata_uses_the_public_base_url` |
| G5 | Clients, codes and tokens live in SQLite on the volume | `test_app.py::test_full_oauth_flow_and_refresh_rotation` plus the `DATA_DIR` resolution tests |
| G6 | 24 h access tokens, refresh rotation with a 10-minute grace window | `test_app.py::test_full_oauth_flow_and_refresh_rotation` (the retried refresh still works) |
| G7 | CSRF bound to the transaction, constant-time compare, 5 failures per IP / 20 globally, Telegram alert | `test_app.py::test_login_lockout_after_repeated_failures` |
| G8 | Fingerprint dedupe inside `DEDUP_WINDOW_MIN` | `test_golden.py::test_15_duplicate_submit_returns_the_same_setup`, `test_tools.py::test_duplicate_submit_through_the_tool` |
| G9 | G-06 price drift and G-07 level range guards, plus tool descriptions | `test_golden.py::test_08_hallucinated_prices_are_rejected` |
| G10 | The snapshot prints New York times; `setup_submit` takes `pda_formed_at_ny` | `test_timeutil.py` DST tests, `test_golden.py::test_16_dst_days_keep_the_kill_zone_and_expiry` |
| G11 | Gemini's one-tap confirmation is documented | `docs/SETUP_SQ.md` step 10, `docs/GEMINI_V11_ADDENDUM.md` section D |
| G12 | Fixed candle counts, compact arrays, 15-second cache | `test_tools.py::test_snapshot_is_compact_and_new_york_timed` (< 60 KB) |
| G13 | `/mcp` is the exact documented URL | `test_app.py::test_mcp_path_without_trailing_slash_is_the_endpoint` |
| G14 | Background tasks start from the lifespan, once | `test_app.py::test_background_tasks_start_once_across_many_requests` |

## cTrader data

| # | Mitigation in the code | Proof |
|---|---|---|
| C1 | `ALLOWED_TOOLS` is enforced inside `call()`; no override exists | `test_ctrader.py::test_trading_tools_are_refused_without_touching_the_network`, `::test_trading_tool_names_appear_only_in_the_denylist_constant`, `::test_no_trading_call_was_recorded_by_any_test` |
| C2 | Digits from metadata, then `PRICE_DIGITS`, then band calibration, plus an M1 cross-check | `test_ctrader.py::test_pipettes_are_decoded_with_the_symbol_digits` and the three decoding tests after it |
| C3 | Symbol ids are validated against the cache before batching | `test_ctrader.py::test_unknown_symbol_ids_never_reach_the_batch` |
| C4 | Only the nine documented periods are ever sent | `test_ctrader.py::test_unsupported_timeframe_is_refused_before_the_call` |
| C5 | History requests are chunked at 720 hours | `test_ctrader.py::test_wide_history_windows_are_chunked` |
| C6 | `CandleStore.merge` drops any bar that is not closed, and the adapter drops future closes | `test_market.py::test_store_drops_forming_candles`, `test_ctrader.py::test_candles_are_decoded_and_forming_bars_dropped` |
| C7 | `AuthError` pauses the engine, alerts the owner and is fixed with `/ctrader` | `test_ctrader.py::test_auth_style_errors_are_classified_as_auth`, `::test_hot_swap_reconnects_and_rediscovers`, `test_golden.py::test_05a_outage_over_the_trigger_bar_becomes_missed` |
| C8 | Token-bucket limiter (20/s general, 4/s historical) and backoff | `test_ctrader.py::test_rate_limiter_spaces_historical_calls`, `test_failure_modes.py::test_rate_limit_errors_are_data_errors_not_crashes` |
| C9 | `classify()` reads the whole exception chain and the error text of an `is_error` result | `test_ctrader.py::test_plain_string_errors_are_handled` |
| C10 | `SYMBOL_MAP` → exact → case-insensitive → unique prefix, with a warning when ambiguous | `test_ctrader.py::test_discovery_records_the_profile_and_never_calls_trading_tools` |
| C11 | Every message says "IC Markets cTrader"; `SETUP_SQ.md` tells the owner to execute there | `test_telegram.py::test_enter_message_shows_the_chase_limit_and_validity`, `docs/SETUP_SQ.md` |
| C12 | Market-hours schedule; outage alerts are suppressed while the market is closed | `test_timeutil.py::test_market_hours_gold`, `::test_daily_break_applies_to_btc` |
| C13 | Candle timestamps are treated as open times and checked in `/selftest` | `test_market.py::test_store_merges_by_open_timestamp`, `runtime.selftest()` alignment lines |

## Engine / validation

| # | Mitigation in the code | Proof |
|---|---|---|
| E1 | State change, audit event and outbox row share one transaction; the dedupe key is unique | `test_golden.py::test_06_restart_between_transaction_and_send_delivers_exactly_once` |
| E2 | Replay turns a late confirmation into MISSED unless it is within `LATE_TRIGGER_MAX_S` | `test_golden.py::test_05a_...`, `::test_05b_short_outage_still_enters_with_a_delay_note` |
| E3 | T-07 refuses a quote older than `QUOTE_MAX_AGE_S` | `test_failure_modes.py::test_enter_is_refused_on_a_stale_quote` |
| E4 | T-06 spread cap, time blocks and the news blackout | `test_golden.py::test_14_spread_spike_over_two_bars_is_missed` |
| E5 | The ENTER message carries the chase limit and a five-minute validity | `test_telegram.py::test_enter_message_shows_the_chase_limit_and_validity` |
| E6 | Invalidation needs a body close on the respect timeframe; wicks are allowed | `test_rules.py::test_l02_and_l03_use_body_closes`, `test_golden.py::test_11b_wick_below_ce_does_not_trigger` |
| E7 | Every window is computed in `America/New_York` | `test_timeutil.py` DST tests, `test_golden.py::test_16_...` |
| E8 | A candle that touches both TP and SL counts as SL | `test_engine.py::test_same_candle_tp_and_sl_counts_as_sl` |
| E9 | Expiry is clamped to the Friday cutoff and the daily close | `test_failure_modes.py::test_gold_setups_expire_at_the_friday_cutoff` |
| E10 | `BTC_WEEKEND_ENTRIES` is off by default | `test_failure_modes.py::test_btc_weekend_entries_are_off_by_default` |
| E11 | Comparisons use full precision; rounding happens only in message text | `test_failure_modes.py::test_rounding_never_changes_a_decision` |
| E12 | `/selftest` warns when the quote timestamp is more than 5 s from the server clock | `runtime.selftest()` |
| E13 | The news cache fails open and the ENTER message says so | `test_news.py::test_a_broken_feed_never_blocks_a_trade`, `::test_fetch_failure_marks_the_cache_not_ok` |

## Telegram

| # | Mitigation in the code | Proof |
|---|---|---|
| T1 | `parse_mode=HTML` with every dynamic value escaped | `test_telegram.py::test_dynamic_values_are_html_escaped` |
| T2 | Messages are split on line boundaries at 4096 characters | `test_telegram.py::test_long_messages_split_on_line_boundaries` |
| T3 | `retry_after` is honoured; the outbox retries | `test_telegram.py::test_sender_honours_retry_after`, `::test_sender_retries_after_a_network_error` |
| T4 | `/start` answers with the chat id while `TELEGRAM_CHAT_ID` is unset | `test_telegram.py::test_start_shows_the_chat_id_when_it_is_not_configured` |
| T5 | Only the owner chat is answered; everyone else is ignored silently | `test_telegram.py::test_strangers_are_ignored_silently` |
| T6 | The `/ctrader` message is deleted after it is read | `test_telegram.py::test_ctrader_message_is_deleted_after_reading` |
| T7 | A logging filter redacts every known secret | `test_telegram.py::test_secrets_never_reach_the_logs` |
