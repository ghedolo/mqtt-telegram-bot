# CONTEXT.md — mqtt-telegram-bot

## Glossary

**Device** — a physical unit that publishes MQTT messages to a single Topic at a regular interval. Identified by a short key in Sensor Config (e.g. `SM2_UTA1`). A Device has one or more Fields. Offline detection is per-Device: if no message arrives on the Topic for 3× the Device interval, an offline Alarm is raised for the Device.

**Field** — a single measurable value published by a Device. A Field is identified by its key within the Device in Sensor Config (e.g. `T`). The canonical Sensor name used throughout the system (DB keys, commands) is derived as `{device_key}_{field_key}`. A Device with a single Field is indistinguishable from a multi-field Device with one entry. Sensor names supplied in commands are resolved case-insensitively to the canonical name (`config.resolve_sensor`); Sensor Config parsing rejects two Sensor names that differ only by case.

**Topic** — the MQTT topic path a Device publishes to. Defined per-Device in Sensor Config. Each Topic must be unique across all Devices.

**Reading** — a single value received for a Field, stored with a timestamp. Keyed by Sensor name in the DB.

**Threshold** — an alarm value set per-Field by an Admin. A Reading above (or below) the Threshold triggers an Alarm. Keyed by Sensor name in the DB.

**Alarm** — an active alert condition. Two types: `threshold` (Field value crossed Threshold) and `offline` (no message on Device Topic for 3× Device interval). Threshold alarms are keyed by Sensor name; offline alarms are keyed by Device key. All alarm events are persisted in the `alarms` table.

**AckOff** — an Admin action that acknowledges an offline Alarm for a Device, suppressing repeat notifications until the Device reconnects (auto-clears on reconnect). Takes the Device key.

**Access Group** — a named set of users (identified by chat_id) defined in credentials config. Referenced by Devices or Fields as `viewers` or `admins`. A user belongs to zero or more Access Groups.

**Viewer** — a member of an Access Group assigned as `viewers` for a Field. Can issue read-only commands for that Field.

**Admin** — a member of an Access Group assigned as `admins` for a Field. Can issue all commands for that Field. Implies Viewer access for the same Field.

**User** — any Telegram user whose chat_id appears in at least one Access Group. Users with no Access Group membership have no access to any Field.

**User Activity** — the last time each User interacted with the bot, recorded in the `user_activity` table (user_id, username, full_name, last_seen). Captured by a global handler that runs on every update before command handlers. Telegram does not expose user last-seen; the bot can only observe interactions directed at it. Queryable by Superadmins via `/usersActivity`.

**Telegram Group** — the single Telegram group where users send commands. The bot never replies with sensor data in the Group; all data replies are sent via DM.

**DM Registration** — the process by which a User activates private messaging with the bot. Triggered by clicking an HMAC-signed deep link sent by the bot in the Telegram Group. Required before the bot can send DM notifications or command replies to that User.

**HMAC Token** — a time-limited (24h TTL) signed token embedded in a Telegram start deep link (`t.me/botname?start=<token>`). Encodes the target chat_id and a timestamp. Verified on `/start` to ensure only the intended User completes DM Registration.

**Sensor Config** — YAML file (`sensors.yaml`) defining Devices under a `devices:` key. Each Device declares its Topic, interval, info label, optional note, and default `viewers`/`admins` Access Group lists. Fields are nested under `fields:` within each Device. Field-level `viewers`/`admins` fully replace (not merge with) Device-level defaults when present. Devices without any `viewers` or `admins` on any Field are visible to nobody (fail-closed).

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
| `/myid` | Show own Telegram user ID |
| `/help` | Show command list (admin-aware) |

### Admin-only commands
| Command | Description |
|---|---|
| `/setAlarm <name> <value>` | Set alarm threshold for a Field (Sensor name) |
| `/ackOff <device>` | Acknowledge offline alarm for a Device (suppresses repeats until Device reconnects) |
| `/forgetSensor <device>` | Archive all readings for a Device to history; clear alarms, threshold, silence state |

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
