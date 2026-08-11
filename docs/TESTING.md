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
- `test_migration_keeps_low_thresholds` — the NOT-NULL-drop rebuild of `thresholds` carries the `low` column across. Copying only `(sensor, value)` before the `DROP` destroyed every low threshold on the first startup after upgrading, once and unrecoverably; every other db test starts from a fresh schema and never walks this path.
- `test_thresholds_set_and_partial_clear` — high/low set; clearing one keeps the row, clearing the last drops it.
- `test_mute_expiry` — active mute is honoured; re-muting into the past expires it.
- `test_forget_device_archives_and_clears` — readings archived, threshold cleared.
- `test_digest_subscriptions_roundtrip` — subscribe (idempotent) / unsubscribe.
- `test_silence_roundtrip` / `test_silence_is_per_key` — the offline-ack (silence) flag set/clear, keyed independently (behind `/ackOff` and auto-clear on reconnect).
- `test_list_silenced_reports_keys_and_ts_oldest_first` — lists every silenced key with its `silenced_at`, oldest first, dropping cleared keys (backs `/ackOff` with no argument).
- `test_get_last_alarms_order_and_sensor_filter` — alarm history newest-first, limited, optionally filtered to one sensor.
- `test_get_last_alarms_across_subjects` — the newest events across a *set* of alarm subjects (a Field plus the Device owning it), newest-first with the limit applied to the whole set; backs `/last5Alarm`, which must mix threshold and offline history.
- `test_get_alarms_since_filters_by_sensor_and_time` — alarms since a timestamp, filtered by sensor list (empty list → no rows).
- `test_record_activity_upserts_and_orders` — user-activity upsert (one row per user) ordered by last-seen (behind `/usersActivity`).

### `tests/test_config.py` — config loading & validation (`bot/config.py`)
- `test_basic_parse_and_derived_names` — `{device}_{field}` names, defaults inherited.
- `test_defaults_and_new_keys` — `retention_days`, `archive_time` (12:00), `enable_menu` (True), `digest_time`.
- `test_field_viewers_override_replaces_device` — field-level viewers replace device-level.
- `test_field_declaring_only_admins_warns` / `..._only_viewers_warns` — a field naming one key alone still loads (the bot must not refuse to start over an access nit) but records a config warning, and the assertions confirm the other key really is blanked, so the warning is not cosmetic.
- `test_clean_config_has_no_warnings` — the warning list stays empty for a well-formed config.
- `test_field_stating_both_replaces_device_lists` — with both keys stated, the device lists are dropped entirely: the device's admin group loses the field too, a sibling field is untouched.
- `test_field_with_neither_key_inherits_both` — no keys → both inherited, the common case.
- `test_empty_access_lists_parse_as_empty_not_missing` — `viewers:` (YAML `None`) and `viewers: []` both mean "no groups", and neither counts as an absent key.
- `test_device_empty_access_lists_parse_as_empty_not_missing` — the same at device level, where a bare `viewers:` used to hit `list(None)` and abort the load instead of being inherited as empty.
- `test_access_helpers` — `is_viewer` / `is_superadmin`.
- `test_duplicate_device_key_across_files` — duplicate device key is a hard error.
- `test_case_insensitive_name_collision` — sensor names differing only by case rejected.
- `test_decimals_out_of_range` — `decimals` must be 0–5.
- `test_interval_must_be_positive` / `test_field_interval_must_be_positive` — an `interval` of zero or less is refused at load; `3 × 0` would mark the device offline forever.
- `test_valid_range_must_not_be_inverted` / `test_valid_range_equal_bounds_allowed` — a `validMin` above its `validMax` is refused (it would discard every real reading as a glitch, silencing alarms on a sensor that looks configured); equal bounds stay legal.
- `test_device_keys_differing_only_by_case_rejected` — device keys get the case-collision check sensor names already had, since `resolve_device` matches case-insensitively.
- `test_unknown_group_name_warns_not_raises` — a group name absent from `credentials.yaml` is fail-closed *and* reported as a config warning, rather than granting nothing in silence.
- `test_stray_key_in_non_defaults_file` — only `00-defaults.yaml` may carry `defaults:`.
- `test_blackout_valid` — a well-formed blackout group parses.
- `test_blackout_unknown_field` — blackout referencing an unknown sensor rejected.
- `test_blackout_below_must_be_positive` — `below` must be > 0.
- `test_blackout_collides_with_sensor_name` — group id can't equal a sensor name.
- `test_blackout_collides_with_device_key` — nor a device key: both are alarm subjects in the same column, so a reused id would fold a device's offline history into the group's blackout history.
- `test_blackout_stale_after_must_be_positive` / `test_blackout_for_seconds_negative_rejected` — numeric bounds enforced.
- `test_blackout_viewers_resolved_from_watched_fields` — blackout viewers resolved from the watched fields' device viewers.
- `test_duplicate_topic_rejected` / `test_field_without_topic_rejected` — topic rules.
- `test_mqtt_tls_inferred_from_port_8883` / `test_mqtt_tls_off_on_plain_port` — TLS inferred from port.
- `test_mqtt_tls_quoted_false_is_off` / `test_mqtt_tls_garbage_rejected` — a quoted `"false"` means off (plain `bool()` made it *on*), and an unparseable value is refused instead of guessed.
- `test_schedule_times_validated_at_load` / `..._out_of_range_rejected` / `..._normalised` — `digest_time`/`archive_time` are validated (and zero-padded) at load. Left to first use they raise inside an asyncio task, killing the digest or archive loop while the bot stays up and says nothing.
- `test_poll_interval_clamped` — clamped to 1–10.
- `test_group_ids_coerced_to_int` — group/superadmin ids coerced to int.
- `test_empty_group_parses_as_no_members` (three YAML spellings) / `test_empty_superadmin_list_is_allowed` — a group with no members is legal however it is written (`[]`, a bare key, or a key with a lone `-`). Two of those spellings used to raise `TypeError` and stop the whole config from loading, so the bot did not start.
- `test_non_numeric_group_member_rejected` / `test_group_that_is_not_a_list_rejected` — tolerating the empty spellings must not tolerate a typo: a non-numeric id and a group that is not a list are still refused.
- `test_resolve_device_is_case_insensitive_and_canonical` — a device key resolves to its canonical form regardless of case; unknown keys pass through unchanged (names are case-preserving but case-insensitive).
- `test_resolve_sensor_is_case_insensitive` — same for sensor names.

### `tests/test_alarm.py` — alarm logic (`bot/alarm_manager.py`)
- `test_threshold_raise_gate_repeat_recover` — 🔴 on first cross, no repeat within `threshold_repeat`, repeats after it, single 🟢 on recovery.
- `test_threshold_low` — low-threshold raise + recovery.
- `test_threshold_none_set_no_alarm` — no threshold configured → silent.
- `test_reset_only_forgets_the_band_it_was_given` / `test_reset_with_no_kind_forgets_both` — `reset_sensor_alarm` clears only the band the caller changed. Resetting both from a `/setAlarm` dropped a live low alarm's `active` flag, and since the recovery branch keys off it, no 🟢 and no `OK_LOW` row ever followed.
- `test_offline_then_recovery` — OFFLINE after `3×interval` of silence, ONLINE when data returns.
- `test_offline_suppressed_during_startup_grace` — no offline alarm during the initial grace window.
- `test_device_added_by_reload_gets_its_own_grace` / `test_run_offline_checks_stamps_devices_once` — a device added by `/reloadConfig` starts its grace when it appears (MQTT has not subscribed to it until a restart, so measuring from process start reported it OFFLINE immediately and kept repeating), and the stamp is never refreshed on later passes, which would renew the grace every 60s and hide a genuinely dead device.
- `test_ackoff_suppresses_repeats_then_auto_clears_on_reconnect` — after `/ackOff`, offline repeats are suppressed while silenced, then the silence flag auto-clears when the device reconnects. Regression: the old `is_silenced` early-return short-circuited the reconnect branch, so silence never cleared.
- `test_ackoff_while_online_does_not_mute_future_offline` — acking a device with no live outage drops the stale silence flag so it can't swallow the next genuine offline alarm.
- `test_blackout_not_raised_until_sustained` — all-dark but below `for_seconds` → no alarm.
- `test_blackout_lifecycle_raise_hold_end` — raise on sustained all-dark; **hold** (no false recovery) when one meter goes stale mid-outage; END only on a confirmed LIT reading; recovery resets the sustain timer.
- `test_blackout_for_seconds_zero_raises_immediately` — `for_seconds: 0` raises on the first dark reading.
- `test_blackout_repeat_notification` — re-notifies "still no current" only after `repeat_seconds`.
- `test_blackout_all_stale_never_raises` — all fields stale (UNKNOWN) → never raised.
- `test_blackout_ignores_out_of_range_reading` — an out-of-range sample is UNKNOWN, not DARK: the glitch filter that protects threshold alarms now protects blackout evidence too, so a single corrupted near-zero reading cannot arm the sustain timer.
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
- `test_oversized_availability_payload_dropped` — the size cap is applied before the availability branch too; it used to sit after, so those topics decoded and JSON-parsed a payload of any size.

### `tests/test_rename_device.py` — the DB half of a device rename (`rename_device.py`)
- `test_mute_follows_the_rename` — a `/silent` mute moves with the sensor. Left behind under the old name it stops matching, and the user's threshold-alarm DMs resume without them asking.
- `test_every_sensor_keyed_table_is_covered` — the schema is the source of truth: every table with a `sensor` column must appear in `SENSOR_TABLES`, so a table added to `bot/db.py` cannot silently fall out of the rename (which is how `mutes` was missed).
- `test_dry_run_changes_nothing` — `--dry-run` rolls back.

### `tests/test_split_device.py` — splitting one device into two (`split_device.py`)
- `test_moved_fields_leave_the_old_device` / `test_new_device_inherits_device_level_keys` / `test_field_bodies_are_carried_over_intact` — the moved fields land under the new key with their bodies and comments intact, and the new device inherits the old one's device-level keys (info, viewers, admins).
- `test_other_devices_are_untouched` — the new block is inserted after the old one, not over the device that followed it.
- `test_moving_every_field_is_refused` / `test_unknown_field_is_refused` / `test_unknown_device_is_refused` — moving *all* fields is a rename (`rename_device.py`), and would leave a device with an empty `fields:`.
- `test_a_file_named_after_the_device_gets_a_file_of_its_own` / `test_a_shared_file_keeps_both_devices` — where the new block lands follows the tree's layout: `SM1_UTA1.yaml` → a new `SM1_CDZ1.yaml` beside it, a multi-device file keeps both.
- `test_separate_file_dry_run_writes_nothing` / `test_existing_target_file_is_refused` — the dry-run creates no file, and an existing target is never overwritten.
- `test_mapping_excludes_the_bare_device_key` — the old key survives a split, so its device-level rows (OFFLINE alarms, ackOff silence) stay put; remapping them would hand one unit's outage history to the other.
- `test_reference_rewrite_is_word_bounded` — a blackout group's `fields:` list names sensors in full and must follow the split, but `SM1_UTA1_I` is a prefix of `SM1_UTA1_IF`: an unbounded replace corrupts the longer name. A stale name there is a hard config-load error, i.e. a bot that will not start.
- `test_reference_rewrite_handles_a_flow_sequence` — the real config writes that list as a one-line flow sequence with a trailing comment, of which only some entries move; the changed lines are printed as a before/after pair, since "would update `<file>`" alone gives the operator nothing to review a partial rewrite against.
- `test_dry_run_reference_rewrite_writes_nothing` — `--dry-run` leaves the file byte-identical while still reporting it, so the end-of-run file summary counts it.
- `test_config_done_migrates_the_db_after_the_yaml_half` / `test_config_done_requires_skip_yaml` — catching the DB up when the YAML was split first: every config-based check has nothing left to validate against, so `--config-done` takes the mapping from `--fields` alone, and refuses to run without `--skip-yaml`.
- `test_db_migration_moves_only_the_split_fields` — readings/threshold/mute/digest rows follow the moved fields; the fields that stayed, and the old device's own offline state, do not move.

### `tests/test_extract_device.py` — moving a device to its own file (`extract_device.py`)
- `test_the_block_moves_out_whole` / `test_extracting_the_first_of_three_keeps_the_rest` — the block runs to the next device key, not to end of file, and the devices that stay keep their bodies and comments.
- `test_dry_run_writes_nothing` / `test_existing_target_is_never_overwritten` — it rewrites hand-maintained YAML in place, so neither file is touched when the run is a dry-run or the target is taken.
- `test_a_device_already_alone_is_refused` — extracting it would leave a file holding `devices:` and nothing else, which the loader rejects.
- `test_unknown_device_is_refused` — a typo in the key stops the run rather than rewriting the wrong file.

### `tests/test_cadence.py` — measuring publish cadence (`cadence.py`)
- `test_a_62s_meter_is_not_rounded_to_a_whole_minute` — the suggested `interval` rounds up proportionally: 62 → 70, not 120, which would double that meter's OFFLINE delay for the sake of a round number.
- `test_measured_cadence_matches_the_data` / `test_a_single_reading_yields_no_cadence` — median and worst gap come out of the real timestamps; one reading yields no cadence rather than a bogus one.
- `test_the_db_is_opened_read_only` — the script's READ-ONLY promise: a diagnostic run against production cannot mutate the readings it measures.
- `test_patterns_filter_by_glob` — sensor selection by glob, and the empty-pattern (all sensors) case.
- `test_report_flags_a_gap_that_would_have_alarmed` — a worst gap above `3 × suggested` is flagged, since adopting that interval would have raised an OFFLINE for it.

### `tests/test_ingest.py` — reading path integration (`bot/ingest.py`)
Wires `process_reading` to the real DB and a real `AlarmManager` (only the
notifiers are stubbed) and drives one full flow.
- `test_reading_rounded_before_storage` — value rounded to the field's `decimals` before it is stored.
- `test_out_of_range_stored_but_not_alarmed` — a reading outside `validMin/Max` is persisted but skips alarm checks (glitch never alarms).
- `test_threshold_alarm_end_to_end` — an in-range reading over threshold produces a 🔴 notification carrying the formatted value.
- `test_blackout_evaluated_on_reading` — a dark current reading re-evaluates and raises its blackout group.
- `test_signal_not_stored_but_feeds_blackout` — a Signal's reading never reaches the DB yet still drives blackout evaluation, the whole point of the Signal split.

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
- `test_stale_threshold_follows_each_sensor_interval` / `test_zigbee_availability_wins_over_data_cadence` / `test_availability_ignored_for_device_without_topic` / `test_stale_column_without_availability_hook` — the `∞` in `/get`'s `min ago` column follows the same rule as the offline alarm: `3 × interval` per sensor (so a 1-minute sensor goes `∞` where an hourly one still prints minutes), overridden by zigbee2mqtt availability only for devices that publish an availability topic, and falling back to cadence when no availability hook is wired.
- `test_listsignal_*` — `/listSignal` rendering: admin sees live signal value, viewer hides it, subscription state flips the hint, outsider sees nothing.
- `test_render_sysinfo` / `..._no_mqtt` — `/sysinfo` summary text, with and without a last-MQTT timestamp.
- `test_render_sysinfo_surfaces_config_warnings` — non-fatal config warnings are appended to `/sysinfo`, so they reach a human rather than only a log nobody tails.
- `test_unknown_command_*` — unknown-command reply only to a registered/addressed user; ignored for other bots and unregistered users.

**Command handlers (end-to-end)**

The `hbot` fixture builds a bot whose config has an admin group, a viewer-only
group, and a superadmin; helpers `_run`/`_run_files` drive a handler as a given
user and return what was sent. Constants `ADMIN` / `VIEWER` / `OUTSIDER` / `SUPER`.
- `test_setalarm_*` / `test_setalarmlow_*` — admin sets high/low threshold; case-insensitive sensor; viewer rejected (not authorized); outsider gets "unknown sensor"; non-numeric rejected; inverted band rejected.
- `test_clearalarm_*` / `test_clearalarmlow_*` — admin clears; viewer left untouched (not authorized).
- `test_ackoff_*` — admin silences a device; case-insensitive device key; viewer not authorized; unknown device; no-arg lists active acks (or "no active"); backs `/ackOff`.
- `test_ackoff_does_not_leak_device_existence` / `test_ackoff_viewer_told_it_lacks_admin` — a caller in no Access Group gets byte-identical refusals for a real and an invented device key, while someone who already views the device is told plainly that they lack admin.
- `test_ackoff_no_args_scoped_to_visible_devices` / `..._viewer_sees_own_device` / `..._superadmin_sees_everything` / `..._outsider_not_authorized` — the no-arg listing is a read: a viewer is enough, a device the caller sees no sensor of is filtered out, a superadmin gets the whole installation, and someone in no access group is refused rather than shown device keys.
- `test_forgetsensor_*` — superadmin-only; case-insensitive device key.
- `test_silent_*` — `/silent`: mute for N hours, clamp to 24h, unmute, no-arg list, per-user isolation.
- `test_digest_*` — `/digest` subscribe on / unsubscribe off, no-arg list (visible only), bad usage.
- `test_list_*` / `test_get_*` — `/list` shows a device reading and is empty for an outsider; `/get` renders a named sensor and reports "no matching" for an unknown one.
- `test_lastseen_*` — `/lastSeen` dates a 3-day-old reading with an absolute timestamp (where `/get` would only print `∞`), lists sensors that never reported as `never` when called with no args, reports "no matching" for an unknown name, and never leaks a sensor the caller cannot view.
- `test_getalarm_*` — `/getAlarm` renders the low/high band; unknown sensor rejected.
- `test_lastalarms_*` / `test_last5alarm_*` — recent alarms for a sensor, "no alarms" when none, hours out of range rejected; last-5 named + unknown sensor.
- `test_lastalarms_includes_offline_of_owning_device` / `test_last5alarm_includes_device_offline` — OFFLINE rows are stored under the *Device* key, so a viewer of one of its fields must still get them; before this the offline history was written and never queried by any command.
- `test_lastalarms_hides_offline_of_invisible_device` — the expansion to Device subjects must not become a leak: a device the caller sees no field of stays out, while the listing is non-empty (guarding against a test that passes only because nothing was returned).
- `test_lastalarms_subscribed_blackout_group` / `..._unsubscribed_blackout_group_excluded` / `..._blackout_group_named_in_expr` / `..._blackout_group_hidden_from_outsider` — blackout history follows the `/digest` opt-in with no args, is reachable by naming the group id, and is refused to a user who may not view the group.
- `test_usersactivity_*` / `test_dbstats_*` — superadmin-gated; render activity list / DB stats. `test_usersactivity_splits_long_listing` drives 300 users through it: the listing is split across messages, each within Telegram's 4096-char limit and with nothing dropped — one oversized send raises `BadRequest`, which nothing catches, so the superadmin used to get no reply at all.
- `test_reloadconfig_*` — superadmin-gated; "not configured" when no reload hook; success path swaps config. `test_reloadconfig_refreshes_signals` covers the Signal table specifically: it gates `viewers_of`/`admins_of`/`is_signal`, and was the one table the reload forgot, so revoking access left the revoked user seeing the live value in `/listSignal` until a restart.
- `test_setalarm_rejects_signal_name` / `test_setalarmlow_*` / `test_clearalarm_*` / `test_getalarm_*` / `test_last5alarm_rejects_signal_name` — a Signal name is refused by every threshold command. `is_viewer`/`is_admin` fall back to the Signal table, so before the `_viewable_sensor` guard an admin of a Signal's device could store a threshold for a field whose readings are never stored, and `/getAlarm` would then display it.
- `test_graph_*` / `test_csv_*` / `test_xlsx_*` — export handlers send a photo/document, clamp admin hours to 72h, and report "no data" / "no matching" appropriately.
- `test_start_*` — `/start` registers the DM with no args, registers on a valid token, and refuses a token minted for a different sender.
- `test_on_arg_reply_*` — the ForceReply follow-up routes a pending command's typed argument to its handler, ignores an expired pending entry, and (via `_consumes_edited_argument`) still dispatches when the argument arrives as an edit, since `_on_arg_reply` reads `effective_message`.
- `test_arg_prompt_is_scoped_to_its_chat` — prompts are booked under `(chat_id, message_id)`. The fake app hands out per-chat ids the way Telegram does, so replying in one chat to an id another chat also uses dispatches nothing; with the old bare-`message_id` key it ran the other user's command.
- `test_answering_older_prompt_keeps_newer_one_alive` — with two prompts open, answering the older one by reply leaves the newer one's fallback tracker intact, and its plain-text answer still dispatches; the unconditional `_pending` clear used to orphan it, swallowing the follow-up with no reply.
- `test_anonymous_sender_does_not_crash` — parametrised over the handlers reachable from a group: an update with `effective_user is None` (anonymous group admin) must return quietly instead of raising `AttributeError` and leaving the command unanswered.
- `test_notify_sensor_gated_by_registration_and_mute` / `test_notify_device_requires_subscription` — DM fan-out honours registration, per-user mutes, and digest subscriptions.
- `test_help_*` — `/help` appends the admin section only for admins and the superadmin section only for superadmins.
- `test_exprsyntax_replies` / `test_listsignal_replies` — thin wrappers reply with non-empty text.
- `test_edited_*` — an edited message is an `edited_message` update, which PTB resolves via `effective_message`. Telegram Web reissues a command by editing the previous bubble, so handlers must process edits like new messages: an edited command re-fires, an edited unknown command still hits the catch-all, and an edited plain text still routes to the arg handler. These drive the handlers' real `check_update`, so they guard the registration itself, not a copy of the filter.
- `test_trace_*` — the `traceCmd` command trace. `_traced` returns the handler untouched when the trace is off (no wrapper, no overhead), and when on it books an in-line and an out-line to the `bot.cmdtrace` logger — sender, command text, and `ok`+elapsed — while a raising handler logs `FAILED` with the exception and re-raises rather than swallowing it. `_labels_unknown_command` pins that the catch-all's `ok_label="unknown"` makes a non-existent command read as `unknown`, not `ok`; `_marks_no_result_via_helper` / `_marks_bad_input_via_helper` / `_marks_denied_via_helper` pin that `_reply_no_match`/`_reply_bad_input`/`_reply_denied` tag the outcome (`no-result` / `bad-input` / `denied`) through the per-invocation ContextVar, end-to-end through `_traced`. Config parsing of `traceCmd`/`traceCmdFile` (defaults and explicit) is in `test_config.py::test_trace_cmd_opts_parse`.

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

### `tests/test_main.py` — startup wiring (`bot/main.py`)
- `test_digest_recipients_survives_a_db_error` / `..._passes_rows_through` — the digest's recipient lookup returns an empty list and logs on failure instead of raising. It runs inside the digest task's `while True`, where one transient DB error used to kill the task outright: the daily digest then never fired again, the exception surfacing only at shutdown.
- `test_trace_off_attaches_nothing` — `_setup_cmd_trace` is a no-op when `traceCmd` is off.
- `test_trace_creates_parent_and_writes` — when on, it creates a missing parent dir and the `bot.cmdtrace` logger actually writes to the file.
- `test_trace_failopen_does_not_raise` — a path that can't be opened (parent is a regular file) warns and returns without attaching a handler, never raising. Guards the read-only-`/app` startup crash: a monitoring bot must not refuse to start over a diagnostic file.
