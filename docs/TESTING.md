# Testing

The **pytest** suite runs on the **development machine**, never inside the
production container. It exercises the pure logic (config parsing, DB, alarms,
MQTT payload parsing, scheduling) against throwaway SQLite/YAML created under
pytest's `tmp_path`, so it never touches `data/sensors.db` or any real config.

> **`pytest -q` is the source of truth** for the list and count of tests. The
> catalogue below is a curated overview and may lag newly added tests — don't
> treat it as an exhaustive, always-current index (that's what the test files
> and `pytest` are for).

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
- `test_silence_roundtrip` / `test_silence_is_per_key` — the offline-ack (silence) flag set/clear, keyed independently (behind `/ackOff` and auto-clear on reconnect).
- `test_list_silenced_reports_keys_and_ts_oldest_first` — lists every silenced key with its `silenced_at`, oldest first, dropping cleared keys (backs `/ackOff` with no argument).
- `test_get_last_alarms_order_and_sensor_filter` — alarm history newest-first, limited, optionally filtered to one sensor.
- `test_get_alarms_since_filters_by_sensor_and_time` — alarms since a timestamp, filtered by sensor list (empty list → no rows).
- `test_record_activity_upserts_and_orders` — user-activity upsert (one row per user) ordered by last-seen (behind `/usersActivity`).

### `tests/test_config.py` — config loading & validation (`bot/config.py`)
- `test_basic_parse_and_derived_names` — `{device}_{field}` names, defaults inherited.
- `test_defaults_and_new_keys` — `retention_days`, `archive_time` (12:00), `enable_menu` (True), `digest_time`.
- `test_field_viewers_override_replaces_device` — field-level viewers replace device-level.
- `test_field_admins_only_drops_inherited_viewers` — the override is all-or-nothing on the pair: a field declaring only `admins:` also discards the device-level `viewers:`, so the device's viewer group loses that one field. Reads like a per-key merge and isn't one.
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
- `test_resolve_device_is_case_insensitive_and_canonical` — a device key resolves to its canonical form regardless of case; unknown keys pass through unchanged (names are case-preserving but case-insensitive).
- `test_resolve_sensor_is_case_insensitive` — same for sensor names.

### `tests/test_alarm.py` — alarm logic (`bot/alarm_manager.py`)
- `test_threshold_raise_gate_repeat_recover` — 🔴 on first cross, no repeat within `threshold_repeat`, repeats after it, single 🟢 on recovery.
- `test_threshold_low` — low-threshold raise + recovery.
- `test_threshold_none_set_no_alarm` — no threshold configured → silent.
- `test_offline_then_recovery` — OFFLINE after `3×interval` of silence, ONLINE when data returns.
- `test_offline_suppressed_during_startup_grace` — no offline alarm during the initial grace window.
- `test_ackoff_suppresses_repeats_then_auto_clears_on_reconnect` — after `/ackOff`, offline repeats are suppressed while silenced, then the silence flag auto-clears when the device reconnects. Regression: the old `is_silenced` early-return short-circuited the reconnect branch, so silence never cleared.
- `test_ackoff_while_online_does_not_mute_future_offline` — acking a device with no live outage drops the stale silence flag so it can't swallow the next genuine offline alarm.
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

### `tests/test_telegram.py` — bot helpers & command handlers (`bot/telegram_bot.py`)
The PTB Application builds offline and never starts polling. Two layers:
pure helpers, and command handlers driven end-to-end with a fake `bot` app
(`_fake_app` records sent messages/photos/documents) so auth checks, argument
parsing, name resolution, and DB side effects are all exercised.

**Pure helpers**
- `test_fmt_ago` / `test_fmt_bytes` — human-readable duration/size formatting.
- `test_threshold_order_ok_when_high_above_low` / `..._ignores_missing_thresholds` / `..._rejects_inverted_band` / `..._rejects_equal_band` — the alarm-band ordering guard: a high threshold must stay strictly above the low one, missing sides never conflict, and inverted or equal bands are rejected (blocks `/setAlarm`/`/setAlarmLow` from creating an incoherent band).
- `test_resolve_sensors_wildcard_respects_visibility` — `*` resolves only to sensors the user may view.
- `test_resolve_sensors_exact_and_hidden` — exact name resolves; a non-visible sensor resolves to nothing.
- `test_resolve_sensors_glob_comma_dedup_caseinsensitive` — glob, comma lists, dedup, case-insensitive matching.
- `test_resolve_blackouts_viewer_gated` — blackout ids resolve only for viewers.
- `test_extract_sort` — `-f`/`-s` flag split, last flag wins.
- `test_apply_sort_alphabetical` / `test_apply_sort_by_field` — `-s` alphabetical vs default field-grouped order.
- `test_token_roundtrip` / `..._wrong_sender` / `..._tampered_signature` / `..._malformed` / `..._expired` — registration-token HMAC accepts a valid token and rejects wrong sender, tampering, garbage, and >24h-old tokens.
- `test_build_digest_only_subscribed_and_visible` — digest lists only sensors both subscribed and visible.
- `test_build_digest_empty_when_no_subscriptions` — no subscriptions → empty string.
- `test_listsignal_*` — `/listSignal` rendering: admin sees live signal value, viewer hides it, subscription state flips the hint, outsider sees nothing.
- `test_render_sysinfo` / `..._no_mqtt` — `/sysinfo` summary text, with and without a last-MQTT timestamp.
- `test_unknown_command_*` — unknown-command reply only to a registered/addressed user; ignored for other bots and unregistered users.

**Command handlers (end-to-end)**

The `hbot` fixture builds a bot whose config has an admin group, a viewer-only
group, and a superadmin; helpers `_run`/`_run_files` drive a handler as a given
user and return what was sent. Constants `ADMIN` / `VIEWER` / `OUTSIDER` / `SUPER`.
- `test_setalarm_*` / `test_setalarmlow_*` — admin sets high/low threshold; case-insensitive sensor; viewer rejected (not authorized); outsider gets "unknown sensor"; non-numeric rejected; inverted band rejected.
- `test_clearalarm_*` / `test_clearalarmlow_*` — admin clears; viewer left untouched (not authorized).
- `test_ackoff_*` — admin silences a device; case-insensitive device key; viewer not authorized; unknown device; no-arg lists active acks (or "no active"); backs `/ackOff`.
- `test_forgetsensor_*` — superadmin-only; case-insensitive device key.
- `test_silent_*` — `/silent`: mute for N hours, clamp to 24h, unmute, no-arg list, per-user isolation.
- `test_digest_*` — `/digest` subscribe on / unsubscribe off, no-arg list (visible only), bad usage.
- `test_list_*` / `test_get_*` — `/list` shows a device reading and is empty for an outsider; `/get` renders a named sensor and reports "no matching" for an unknown one.
- `test_getalarm_*` — `/getAlarm` renders the low/high band; unknown sensor rejected.
- `test_lastalarms_*` / `test_last5alarm_*` — recent alarms for a sensor, "no alarms" when none, hours out of range rejected; last-5 named + unknown sensor.
- `test_usersactivity_*` / `test_dbstats_*` — superadmin-gated; render activity list / DB stats.
- `test_reloadconfig_*` — superadmin-gated; "not configured" when no reload hook; success path swaps config.
- `test_graph_*` / `test_csv_*` / `test_xlsx_*` — export handlers send a photo/document, clamp admin hours to 72h, and report "no data" / "no matching" appropriately.
- `test_start_*` — `/start` registers the DM with no args, registers on a valid token, and refuses a token minted for a different sender.
- `test_on_arg_reply_*` — the ForceReply follow-up routes a pending command's typed argument to its handler, and ignores an expired pending entry.
- `test_notify_sensor_gated_by_registration_and_mute` / `test_notify_device_requires_subscription` — DM fan-out honours registration, per-user mutes, and digest subscriptions.
- `test_help_*` — `/help` appends the admin section only for admins and the superadmin section only for superadmins.
- `test_exprsyntax_replies` / `test_listsignal_replies` — thin wrappers reply with non-empty text.

**Autocomplete menu**

`set_my_commands` is deliberately user-level only — admin/superadmin commands
still work when typed but stay out of the menu. `MENU_EXEMPT` in the test file
lists the privileged commands; the split is pinned so a new *user* command
cannot silently miss the menu (how `/listSignal` was lost).
- `test_menu_commands_are_valid_telegram_names` / `test_menu_has_no_duplicates` — Telegram accepts each name (`[a-z0-9_]{1,32}`, non-empty description ≤256) and no name is listed twice.
- `test_every_menu_command_has_a_handler` — nothing advertised in the menu is missing a `CommandHandler`.
- `test_menu_omits_exactly_the_privileged_commands` — registered minus menu equals `MENU_EXEMPT`; the regression guard.
- `test_menu_contains_no_privileged_command` — no admin/superadmin command leaks into autocomplete.
- `test_listsignal_is_in_the_menu` — `/listSignal` is user-level, so it belongs there.

### `tests/test_schedule.py` — wall-clock scheduling (`bot/schedule.py`)
- `test_next_occurrence_later_today` — target still ahead today.
- `test_next_occurrence_already_passed_rolls_tomorrow` — past target rolls to tomorrow.
- `test_next_occurrence_exactly_now_rolls_tomorrow` — exact-now rolls to tomorrow.
- `test_seconds_until_parses_hhmm` — `HH:MM` → seconds.
- `test_seconds_until_rolls_over_midnight` — correct span across midnight. Guards the `sleep(86400)` archive bug.
