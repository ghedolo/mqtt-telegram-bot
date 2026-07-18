# CONTEXT.md — mqtt-telegram-bot

## Glossary

**Device** — a physical unit that publishes MQTT messages to a single Topic at a regular interval. Identified by a short key in Sensor Config (e.g. `SM2_UTA1`). A Device has one or more Fields. Offline detection is per-Device: if no message arrives on the Topic for 3× the Device interval, an offline Alarm is raised for the Device.

**Field** — a single measurable value published by a Device. A Field is identified by its key within the Device in Sensor Config (e.g. `T`). The canonical Sensor name used throughout the system (DB keys, commands) is derived as `{device_key}_{field_key}`. A Device with a single Field is indistinguishable from a multi-field Device with one entry. Sensor names supplied in commands are resolved case-insensitively to the canonical name (`config.resolve_sensor`); Sensor Config parsing rejects two Sensor names that differ only by case.

**Topic** — the MQTT topic path a Device publishes to. Defined per-Device in Sensor Config. Each Topic must be unique across all Devices.

**Reading** — a single value received for a Field, stored with a timestamp. Keyed by Sensor name in the DB. Always numeric (`readings.value` is `REAL`): a boolean JSON payload (e.g. Zigbee2MQTT `contact: true`/`false`) is ingested as `1.0`/`0.0` via `float()`, so a discrete Field is an ordinary scalar and every threshold/graph/digest path works unchanged. Human-readable labels for such values are a pure *display* concern, not a type: a Field may carry a `states` render table (value → label, e.g. `false: Aperta`) that `config.fmt` consults, falling back to the number for any unmapped value (so an alarm threshold like `0.5` still renders numerically). The same `states` map is **bidirectional** at ingestion: when a payload is a discrete string an enum can't pass through `float()` (e.g. Zigbee2MQTT `illumination: "dim"`/`"bright"`), `states` is used in reverse (label → value) to store it as its numeric code — so an enum Field is stored, thresholded, graphed and rendered like any other discrete Field. The system stays agnostic to what a scalar *means* — there is no notion of Sensor "type".

**Threshold** — an alarm value set per-Field by an Admin. A Reading above (or below) the Threshold triggers an Alarm. Keyed by Sensor name in the DB.

**Alarm** — an active alert condition. Three types: `threshold` (Field value crossed Threshold), `offline` (no message on Device Topic for 3× Device interval), and `blackout` (all current Fields in a Blackout Group below a threshold for a sustained duration). Threshold alarms are keyed by Sensor name; offline alarms by Device key; blackout alarms by Blackout Group id. All alarm events are persisted in the `alarms` table.

**Blackout** — a derived, temporal Alarm (sibling of offline) indicating the monitored loads have lost mains power while the monitoring stack — sensors, MQTT, the bot — keeps running on UPS. Raised when **every** current Field in a Blackout Group has a *fresh* reading (newer than the group's staleness window) below the group's threshold, sustained for the group's duration; a live-but-idle load draws a non-zero baseline (e.g. a CDZ at ~1.6 A), so a near-zero reading means *unpowered*, not idle. Evaluation is event-driven — re-checked on every incoming current reading, so detection latency is roughly the meter's publish cadence, which is also the floor on resolution (a blackout shorter than the publish interval is unobservable). Not a Reading — no value is stored. Ends (emitting an end-of-blackout recovery message, as offline emits its reconnect message) **only on positive proof** — when a Field has a fresh reading at/above the threshold. A Field going stale (its meter stopped) does **not** end the Blackout: the alarm is held and that Field's Device offline Alarm reports the silence, avoiding a false recovery when one meter dies mid-outage while another still reads zero. Keyed by Blackout Group id. Notification is opt-in: a User subscribes to the Blackout Group id itself via `/digest` (the id is a subscribable pseudo-entity, valid as a `/digest` target but carrying no Reading, so it never renders as a value row in `/get` or the daily digest). Any Viewer of at least one watched Field may subscribe; delivery is DM to subscribed Users only — unlike offline (Admin-gated), because subscription is an explicit, self-selected opt-in.

**Dip** (microbuco) — a momentary loss of mains power, too brief to qualify as a sustained Blackout: a single all-dark reading, already recovered by the next one. Not modelled as its own Alarm — it *is* the blackout Alarm, surfaced by configuring a Blackout Group with `for_seconds: 0`, so the **first** all-dark reading raises immediately instead of waiting for a sustain window; a Dip is then just a Blackout whose recovery message follows within one publish cadence. Detectable only while **every** watched meter samples the same dip — the meters must publish near-synchronously — because an event shorter than the publish cadence that falls between one meter's samples leaves that Field LIT, so the group is never all-dark. Inference from current cannot go below this floor; catching arbitrarily short events would need a latching signal (a power-fail contact or a UPS "on battery" topic), not a sampled current.

**Blackout Group** — a named set of current Fields (or Signals) watched together for Blackout detection, declared under `blackouts:` in the defaults file (`00-defaults.yaml`, the only file allowed non-`devices` keys). The id doubles as the blackout Alarm key and appears in messages. Declares the Fields to watch, the current threshold, the sustained duration before raising (`for_seconds`, independent of freshness; `0` = raise on the first all-dark reading, turning the group into a Dip detector), the freshness window (`stale_after`, kept ≥ the meter's publish cadence — and ≥ ~2× the cadence when relying on single-reading Dip detection, so a lagging or dropped message does not leave one Field stale while the other is fresh), and the repeat interval for a persisting blackout.

**Signal** — a Field whose Readings are **never stored**, consumed only as an input to a derived Alarm (Blackout). Declared like any Field but with `signal: true`; it lives outside the Sensor set (`AppConfig.signals`, not `sensors`), so it never appears in `/get`, `/graph`, `/list`, the digest, thresholds, or a Device's offline check. Its latest value is held only in memory (the AlarmManager cache) and read by blackout evaluation in place of a stored Reading. Purpose: sample current at a fast cadence (e.g. a 3 s `IF`) to lower the Dip/Blackout resolution floor — a 3 s Signal drops it to ~6 s versus ~124 s for the 62 s slow-current path — without paying the storage cost of persisting every fast sample. A Signal inherits its Device's `viewers`/`admins`, which authorise subscription to the Blackout Group it feeds. Discoverable via `/listSignal`. A Signal is the opposite of a stored discrete Field such as the door contact (`states`): the latter is persisted and graphable, a Signal is neither.

**AckOff** — an Admin action that acknowledges an offline Alarm for a Device, suppressing repeat notifications until the Device reconnects (auto-clears on reconnect). Takes the Device key.

**Mute** — a per-User, time-bounded suppression of **threshold** Alarm DMs for a Field, set via `/silent`. Lasts a whole number of hours, 1–24. While a User has a Field Muted, that User receives no threshold Alarm DM for it; the Alarm itself is unaffected (still fires, still recorded, still delivered to other Admins). Does not affect offline Alarms (see AckOff). Keyed by `(User, Sensor)`; stored with an expiry timestamp.
_Avoid_: silence (reserved for the offline-ack state).

**Access Group** — a named set of users (identified by chat_id) defined in credentials config. Referenced by Devices or Fields as `viewers` or `admins`. A user belongs to zero or more Access Groups.

**Viewer** — a member of an Access Group assigned as `viewers` for a Field. Can issue read-only commands for that Field.

**Admin** — a member of an Access Group assigned as `admins` for a Field. Can issue all commands for that Field. Implies Viewer access for the same Field.

**User** — any Telegram user whose chat_id appears in at least one Access Group. Users with no Access Group membership have no access to any Field.

**User Activity** — the last time each User interacted with the bot, recorded in the `user_activity` table (user_id, username, full_name, last_seen). Captured by a global handler that runs on every update before command handlers. Telegram does not expose user last-seen; the bot can only observe interactions directed at it. Queryable by Superadmins via `/usersActivity`.

**Telegram Group** — the single Telegram group where users send commands. The bot never replies with sensor data in the Group; all data replies are sent via DM.

**DM Registration** — the process by which a User activates private messaging with the bot. Triggered by clicking an HMAC-signed deep link sent by the bot in the Telegram Group. Required before the bot can send DM notifications or command replies to that User.

**HMAC Token** — a time-limited (24h TTL) signed token embedded in a Telegram start deep link (`t.me/botname?start=<token>`). Encodes the target chat_id and a timestamp. Verified on `/start` to ensure only the intended User completes DM Registration.

**Sensor Config** — YAML files in the `sensors.d/` directory (read recursively and merged at startup; duplicate Device keys across files are rejected) defining Devices under a `devices:` key. The shared `defaults:` block is allowed only in `00-defaults.yaml`; every other file must contain nothing but `devices:` (any stray top-level key is a hard error). Each Device declares its Topic, interval, info label, optional note, and default `viewers`/`admins` Access Group lists. Fields are nested under `fields:` within each Device. Field-level `viewers`/`admins` fully replace (not merge with) Device-level defaults when present. Devices without any `viewers` or `admins` on any Field are visible to nobody (fail-closed). A Field marked `signal: true` is parsed as a Signal (never stored) and diverted out of the Sensor set, keeping its name/topic in the shared namespace so collisions are still rejected.

## Bot Commands

### User commands
| Command | Description |
|---|---|
| `/list` | List all visible Devices, one line per Device with all Fields |
| `/get [expr]` | Get Fields matching expr (Sensor name, glob, comma-separated); no arg = digest subscriptions |
| `/getAlarm [name]` | Show alarm threshold(s) for a Field |
| `/graph <name>` | Chart last 8h for a Field (Sensor name) |
| `/last` | Timestamp of the last message received from MQTT, any Topic (no content) |
| `/lastAlarm [name]` | Last alarm event (all or specific Sensor/Device) |
| `/last5Alarm <name>` | Last 5 alarm events for a Sensor or Device |
| `/digest [expr] [on\|off]` | Show or manage per-user digest subscriptions |
| `/listSignal` | List visible Blackout Groups (subscribable), the Signals feeding each (live value for Admins), and your subscription state |
| `/silent [expr] [Nh]` | Mute own threshold Alarm DMs for matching Fields: no args = list active Mutes; expr only = unmute; expr + `Nh` (1–24, clamped) = Mute for N hours |
| `/sysinfo` | Bot version, uptime, memory (RSS/limit), DB size, last-MQTT freshness, device/sensor counts — a non-sensitive health summary for any User |
| `/myid` | Show own Telegram user ID |
| `/help` | Show command list (admin-aware) |

### Admin-only commands
| Command | Description |
|---|---|
| `/setAlarm <name> <value>` | Set high alarm threshold for a Field (Sensor name); alarm if value > threshold |
| `/setAlarmLow <name> <value>` | Set low alarm threshold for a Field (Sensor name); alarm if value < threshold |
| `/ackOff <device>` | Acknowledge offline alarm for a Device (suppresses repeats until Device reconnects) |
| `/forgetSensor <device>` | Archive all readings for a Device to history; clear alarms, threshold, offline-ack state |

### Superadmin-only commands
| Command | Description |
|---|---|
| `/reloadConfig` | Reload Sensor Config and credentials config without restart |
| `/usersActivity` | List last interaction time per User (User Activity) |

**Daily Digest** — a scheduled silent message sent once per day at a configurable time (`digest_time` in credentials config, default `15:00`). Per-user: only Fields the User has subscribed to via `/digest` and can see. Format: `🟢 live since 3d 4h` on first line, then one line per Device as `info: F1=v1 F2=v2 ...` with trailing ` *` if a threshold Alarm occurred on any subscribed Field in the last 24h. Offline Fields show `--` as value.

## Notification behaviour

- Threshold alarm messages sent via DM to Admins of the affected Field.
- Offline alarm messages sent via DM to Admins of Fields for which the User has an active digest subscription in that Device.
- Daily Digest sent via DM to each User, filtered to Fields they have subscribed to via `/digest` and can see.
- Command replies sent via DM, silently (`disable_notification=True`).
- Bot never sends sensor data in the Telegram Group. Group messages are limited to DM Registration prompts.
- If DM Registration is not yet completed for a User, the bot replies in the Telegram Group with a registration prompt and HMAC Token deep link, and no sensor data.
- Bot replies never quote or echo user input (`send_message` not `reply_text`).
- If a Field is not visible to the requesting User, the bot responds as if it does not exist.
- An unrecognised command replies `❓ Unknown command` via DM, but only to a DM-registered User; a command addressed to another bot in a shared group (`/x@otherbot`) and any command from a non-registered sender are ignored (no reply, no registration prompt), so typos and other bots' commands never spam the Group.
