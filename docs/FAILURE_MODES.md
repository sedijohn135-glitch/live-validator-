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
| G5 | Clients, codes and tokens live in SQLite on the volume | `test_app.py::test_tokens_survive_a_restart_on_the_same_database` |
| G6 | 24 h access tokens, refresh rotation with a 10-minute grace window | `test_app.py::test_full_oauth_flow_and_refresh_rotation` (the retried refresh still works) |
| G7 | CSRF bound to the transaction, constant-time compare, 5 failures per IP / 20 globally, Telegram alert | `test_app.py::test_login_lockout_after_repeated_failures` |
| G8 | Each submission is its own setup; the guides tell Gemini to check `setup_status` first | `docs/GEMINI.md` section F, `spark-skill/live-validator/SKILL.md` section E |
| G9 | Prices come from the snapshot; a hallucinated level simply never gets touched | `test_tools.py::test_snapshot_is_compact_and_new_york_timed`, `docs/GEMINI.md` section B |
| G10 | The snapshot prints New York times, and no submitted field carries a time at all | `test_timeutil.py` DST tests, `test_tools.py::test_submit_asks_for_almost_nothing` |
| G11 | Gemini's one-tap confirmation is documented | `docs/SETUP_SQ.md` step 10, `docs/GEMINI.md` section C |
| G12 | Fixed candle counts, compact arrays, 15-second cache | `test_tools.py::test_snapshot_is_compact_and_new_york_timed` (< 60 KB) |
| G13 | `/mcp` is the exact documented URL | `test_app.py::test_mcp_path_without_trailing_slash_is_the_endpoint` |
| G14 | Background tasks start from the lifespan, once | `test_app.py::test_background_tasks_start_once_across_many_requests` |
| G15 | Brave's renderer caches the 'Working on it…' placeholder past the model's final chunk; six cache-busting headers on every `/mcp` response stop it. OPTIONS preflights are skipped so CORS keeps its own Max-Age | `test_app.py::test_mcp_response_carries_no_store_cache_headers` and the four no-store tests below it |

## cTrader data

| # | Mitigation in the code | Proof |
|---|---|---|
| C1 | `ALLOWED_TOOLS` is enforced inside `call()`; no override exists | `test_ctrader.py::test_trading_tools_are_refused_without_touching_the_network`, `::test_trading_tool_names_appear_only_in_the_denylist_constant`, `::test_no_trading_call_was_recorded_by_any_test` |
| C2 | Digits from metadata, then `PRICE_DIGITS`, then band calibration, plus an M1 cross-check | `test_ctrader.py::test_pipettes_are_decoded_with_the_symbol_digits` and the three decoding tests after it |
| C3 | Symbol ids are validated against the cache before batching | `test_ctrader.py::test_unknown_symbol_ids_never_reach_the_batch` |
| C4 | Only the nine documented periods are ever sent | `test_ctrader.py::test_unsupported_timeframe_is_refused_before_the_call` |
| C5 | History requests are chunked at 720 hours | `test_ctrader.py::test_wide_history_windows_are_chunked` |
| C6 | `CandleStore.merge` drops any bar that is not closed, and the adapter drops future closes | `test_market.py::test_store_drops_forming_candles`, `test_ctrader.py::test_candles_are_decoded_and_forming_bars_dropped` |
| C7 | `AuthError` pauses the engine, alerts the owner hourly and is fixed with `/ctrader` | `test_ctrader.py::test_auth_style_errors_are_classified_as_auth`, `test_feed.py::test_the_token_warning_repeats_instead_of_firing_once_forever` |
| C8 | Token-bucket limiter (20/s general, 4/s historical) and backoff | `test_ctrader.py::test_rate_limiter_spaces_historical_calls`, `test_failure_modes.py::test_rate_limit_errors_are_data_errors_not_crashes` |
| C9 | `classify()` reads the whole exception chain and the error text of an `is_error` result | `test_ctrader.py::test_plain_string_errors_are_handled` |
| C10 | `SYMBOL_MAP` → exact → case-insensitive → unique prefix, with a warning when ambiguous | `test_ctrader.py::test_discovery_records_the_profile_and_never_calls_trading_tools` |
| C11 | Prices in every message come from the IC Markets feed the snapshot uses | `test_telegram.py::test_the_enter_message_carries_the_whole_decision`, `docs/SETUP_SQ.md` |
| C12 | Market-hours schedule; outage alerts are suppressed while the market is closed | `test_timeutil.py::test_market_hours_gold`, `::test_daily_break_applies_to_btc` |
| C13 | Candle timestamps are treated as open times and checked in `/selftest` | `test_market.py::test_store_merges_by_open_timestamp`, `runtime.selftest()` alignment lines |

## Engine / validation

| # | Mitigation in the code | Proof |
|---|---|---|
| E1 | State change, audit event and outbox row share one transaction; the dedupe key is unique | `test_engine.py::test_the_full_path_from_registration_to_enter_now` (one message per transition) |
| E2 | Evidence is read from closed M1 candles, so a gap in ticks cannot invent a confirmation | `test_evidence.py::test_the_first_tick_into_the_zone_is_never_enough` |
| E3 | A stale or synthetic quote holds the entry (never cancels it) | `test_failure_modes.py::test_enter_is_held_on_a_stale_quote` |
| E4 | A spread spike or a falling knife holds the entry until the market settles | `test_evidence.py::test_a_wide_spread_holds_the_entry_without_killing_the_setup`, `::test_a_falling_knife_holds_the_entry` |
| E5 | The ENTER message carries the recomputed stop, the targets and the secure level | `test_telegram.py::test_the_enter_message_carries_the_whole_decision` |
| E6 | A price that ran more than 0.35 R becomes a LIMIT instead of a chase | `test_engine.py::test_a_runaway_price_becomes_a_limit_not_a_chase` |
| E7 | Every window in the snapshot is computed in `America/New_York` | `test_timeutil.py` DST tests |
| E8 | The stop is checked before the targets, so a candle through both counts as the stop | `test_engine.py::test_the_stop_after_entry_closes_the_setup` |
| E9 | A setup never expires: only its stop or TP1 can close it | `test_failure_modes.py::test_a_setup_waits_instead_of_expiring_over_the_weekend` |
| E10 | No clock and no calendar can cancel a setup | `test_failure_modes.py::test_no_clock_and_no_calendar_can_cancel_a_setup` |
| E11 | Comparisons use full precision; rounding happens only in message text | `test_failure_modes.py::test_rounding_never_changes_a_decision` |
| E12 | `/selftest` warns when the quote timestamp is more than 5 s from the server clock | `runtime.selftest()` |
| E13 | The recomputed stop is never wider than the submitted one, nor tighter than 0.35 R of it | `test_plan.py::test_the_new_stop_is_never_wider_than_the_one_the_setup_gave`, `::test_the_new_stop_is_never_tighter_than_a_third_of_the_original_risk` |
| E14 | The owner is told where price can turn before TP1, and when to move to break-even | `test_engine.py::test_after_entry_the_owner_is_told_where_to_secure_the_profit` |

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
