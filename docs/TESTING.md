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
- `test_blackout_lifecycle_raise_hold_end` — raise on sustained all-dark; **hold** (no false recovery) when one meter goes stale mid-outage; END only on a confirmed LIT reading.

### `tests/test_mqtt.py` — payload parsing (`bot/mqtt_client.py`)
- `test_plain_float` — plain numeric payload parsed.
- `test_json_path_extraction` — nested value pulled via `json_path`.
- `test_unknown_topic_ignored` — message on an unsubscribed topic dropped.
- `test_non_numeric_plain_dropped` — non-numeric payload dropped, no crash.
- `test_malformed_json_dropped` — invalid JSON dropped.
- `test_json_missing_field_skipped` — absent json field skipped (intermittent field is normal).
- `test_oversized_payload_dropped` — payload over 64 KiB rejected.

### `tests/test_schedule.py` — wall-clock scheduling (`bot/schedule.py`)
- `test_next_occurrence_later_today` — target still ahead today.
- `test_next_occurrence_already_passed_rolls_tomorrow` — past target rolls to tomorrow.
- `test_next_occurrence_exactly_now_rolls_tomorrow` — exact-now rolls to tomorrow.
- `test_seconds_until_parses_hhmm` — `HH:MM` → seconds.
- `test_seconds_until_rolls_over_midnight` — correct span across midnight. Guards the `sleep(86400)` archive bug.
