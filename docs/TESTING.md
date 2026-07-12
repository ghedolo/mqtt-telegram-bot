# Testing

Unit tests run on the **development machine**, never inside the production
container. They exercise the pure logic (config parsing, DB, alarms, MQTT
payload parsing, scheduling) against throwaway SQLite/YAML created under
pytest's `tmp_path`, so they never touch `data/sensors.db` or any real config.

## Running

```bash
python3 -m venv .venv                       # first time only
source .venv/bin/activate
pip install -r requirements-dev.txt         # pytest + runtime deps
python -m pytest
```

Config lives in `pytest.ini` (`pythonpath = .`, `testpaths = tests`). Run the
suite before every push; the deploy on the host (`./deploy.sh`) does **not**
run tests.

## What each test covers

### `tests/test_db.py` — storage & archive (`bot/db.py`)
- `test_insert_and_get_latest` — newest reading wins by timestamp.
- `test_get_latest_missing_sensor` — unknown sensor returns `None`.
- `test_get_history_window_and_order` — only in-window rows, ascending by ts.
- `test_archive_moves_old_keeps_recent` — old rows move to `readings_archive`, recent stay.
- `test_archive_noop_when_all_recent` — nothing archived when all rows are within retention.
- `test_archive_boundary_is_strict` — a row exactly at the cutoff is **kept** (`ts < cutoff`); guards the regression that left the archive empty.
- `test_thresholds_set_and_partial_clear` — high/low set; clearing one keeps the row, clearing the last drops it.
- `test_mute_expiry` — active mute is honoured; re-muting into the past expires it.
- `test_forget_device_archives_and_clears` — readings archived, threshold cleared.
- `test_digest_subscriptions_roundtrip` — subscribe (idempotent) / unsubscribe.

### `tests/test_config.py` — config loading & validation (`bot/config.py`)
- `test_basic_parse_and_derived_names` — `{device}_{field}` names, defaults inherited.
- `test_defaults_and_new_keys` — `retention_days`, `archive_time` (12:00), `enable_menu` (True), `digest_time`.
- `test_field_viewers_override_replaces_device` — field-level viewers replace device-level.
- `test_access_helpers` — `is_viewer` / `is_superadmin`.
- `test_duplicate_device_key_across_files` — duplicate device key is a hard error.
- `test_case_insensitive_name_collision` — sensor names differing only by case rejected.
- `test_decimals_out_of_range` — `decimals` must be 0–5.
- `test_stray_key_in_non_defaults_file` — only `00-defaults.yaml` may carry `defaults:`.
- `test_blackout_valid` — a well-formed blackout group parses.
- `test_blackout_unknown_field` — blackout referencing an unknown sensor rejected.
- `test_blackout_below_must_be_positive` — `below` must be > 0.
- `test_blackout_collides_with_sensor_name` — group id can't equal a sensor name.
- `test_blackout_stale_after_must_be_positive` / `test_blackout_for_seconds_negative_rejected` — numeric bounds enforced.
- `test_blackout_viewers_resolved_from_watched_fields` — blackout viewers resolved from the watched fields' device viewers.
- `test_duplicate_topic_rejected` / `test_field_without_topic_rejected` — topic rules.
- `test_mqtt_tls_inferred_from_port_8883` / `test_mqtt_tls_off_on_plain_port` — TLS inferred from port.
- `test_poll_interval_clamped` — clamped to 1–10.
- `test_group_ids_coerced_to_int` — group/superadmin ids coerced to int.

### `tests/test_alarm.py` — alarm logic (`bot/alarm_manager.py`)
- `test_threshold_raise_gate_repeat_recover` — 🔴 on first cross, no repeat within `threshold_repeat`, repeats after it, single 🟢 on recovery.
- `test_threshold_low` — low-threshold raise + recovery.
- `test_threshold_none_set_no_alarm` — no threshold configured → silent.
- `test_offline_then_recovery` — OFFLINE after `3×interval` of silence, ONLINE when data returns.
- `test_offline_suppressed_during_startup_grace` — no offline alarm during the initial grace window.
- `test_blackout_not_raised_until_sustained` — all-dark but below `for_seconds` → no alarm.
- `test_blackout_lifecycle_raise_hold_end` — raise on sustained all-dark; **hold** (no false recovery) when one meter goes stale mid-outage; END only on a confirmed LIT reading; recovery resets the sustain timer.
- `test_blackout_for_seconds_zero_raises_immediately` — `for_seconds: 0` raises on the first dark reading.
- `test_blackout_repeat_notification` — re-notifies "still no current" only after `repeat_seconds`.
- `test_blackout_all_stale_never_raises` — all fields stale (UNKNOWN) → never raised.
- `test_check_blackout_for_dispatches_only_watching_groups` — an event re-checks only groups watching that sensor.
- `test_blackout_notify_none_is_noop` — no blackout notifier → early return, no state/crash.

### `tests/test_mqtt.py` — payload parsing (`bot/mqtt_client.py`)
- `test_plain_float` — plain numeric payload parsed.
- `test_json_path_extraction` — nested value pulled via `json_path`.
- `test_unknown_topic_ignored` — message on an unsubscribed topic dropped.
- `test_non_numeric_plain_dropped` — non-numeric payload dropped, no crash.
- `test_malformed_json_dropped` — invalid JSON dropped.
- `test_json_missing_field_skipped` — absent json field skipped (intermittent field is normal).
- `test_oversized_payload_dropped` — payload over 64 KiB rejected.

### `tests/test_ingest.py` — reading path integration (`bot/ingest.py`)
Wires `process_reading` to the real DB and a real `AlarmManager` (only the
notifiers are stubbed) and drives one full flow.
- `test_reading_rounded_before_storage` — value rounded to the field's `decimals` before it is stored.
- `test_out_of_range_stored_but_not_alarmed` — a reading outside `validMin/Max` is persisted but skips alarm checks (glitch never alarms).
- `test_threshold_alarm_end_to_end` — an in-range reading over threshold produces a 🔴 notification carrying the formatted value.
- `test_blackout_evaluated_on_reading` — a dark current reading re-evaluates and raises its blackout group.

### `tests/test_graph.py` — chart data prep & rendering (`bot/graph.py`)
- `test_prepare_series_plain` — in-range readings pass through unchanged.
- `test_prepare_series_high_glitch_dropped` / `test_prepare_series_low_glitch_dropped` — readings outside `validMin/Max` become NaN in the line and are recorded as edge markers, not in `in_vals`.
- `test_prepare_series_no_bounds_keeps_everything` — no bounds → nothing filtered.
- `test_prepare_series_gap_inserts_break` — a gap over `interval×2.5` inserts a NaN breakpoint so no segment bridges the silence.
- `test_prepare_series_no_gap_when_within_threshold` — small gap → no break.
- `test_build_renders_png` / `test_build_handles_no_data` / `test_build_multi_sensor_with_glitch` — `build()` returns a valid PNG for normal, empty, and glitchy multi-sensor inputs.

### `tests/test_telegram.py` — bot helpers (`bot/telegram_bot.py`)
Pure helpers only; the PTB Application builds offline and never starts polling.
- `test_fmt_ago` / `test_fmt_bytes` — human-readable duration/size formatting.
- `test_resolve_sensors_wildcard_respects_visibility` — `*` resolves only to sensors the user may view.
- `test_resolve_sensors_exact_and_hidden` — exact name resolves; a non-visible sensor resolves to nothing.
- `test_resolve_sensors_glob_comma_dedup_caseinsensitive` — glob, comma lists, dedup, case-insensitive matching.
- `test_resolve_blackouts_viewer_gated` — blackout ids resolve only for viewers.
- `test_extract_sort` — `-f`/`-s` flag split, last flag wins.
- `test_apply_sort_alphabetical` / `test_apply_sort_by_field` — `-s` alphabetical vs default field-grouped order.
- `test_token_roundtrip` / `..._wrong_sender` / `..._tampered_signature` / `..._malformed` / `..._expired` — registration-token HMAC accepts a valid token and rejects wrong sender, tampering, garbage, and >24h-old tokens.
- `test_build_digest_only_subscribed_and_visible` — digest lists only sensors both subscribed and visible.
- `test_build_digest_empty_when_no_subscriptions` — no subscriptions → empty string.

### `tests/test_schedule.py` — wall-clock scheduling (`bot/schedule.py`)
- `test_next_occurrence_later_today` — target still ahead today.
- `test_next_occurrence_already_passed_rolls_tomorrow` — past target rolls to tomorrow.
- `test_next_occurrence_exactly_now_rolls_tomorrow` — exact-now rolls to tomorrow.
- `test_seconds_until_parses_hhmm` — `HH:MM` → seconds.
- `test_seconds_until_rolls_over_midnight` — correct span across midnight. Guards the `sleep(86400)` archive bug.
