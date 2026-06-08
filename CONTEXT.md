# CONTEXT.md — mqtt-telegram-bot

## Glossary

**Sensor** — a physical device that publishes temperature readings to the MQTT broker at a regular interval. Identified by a short name (e.g. `A`).

**Topic** — the MQTT topic path a Sensor publishes to. Defined per-sensor in config.

**Reading** — a single temperature value received from a Sensor, stored with a timestamp.

**Threshold** — an alarm temperature value set per-sensor by an Admin. A Reading above the Threshold triggers an Alarm.

**Alarm** — an active alert condition on a Sensor. Two types: `threshold` (temperature above Threshold) and `offline` (no Reading received for 3× the Sensor's publish interval). All alarm events are persisted in the `alarms` table.

**AckOff** — an Admin action that acknowledges an offline Alarm, suppressing repeat notifications until the Sensor comes back online (auto-clears on reconnect).

**Access Group** — a named set of users (identified by chat_id) defined in credentials config. Referenced by Sensors as `viewers` or `admins`. A user belongs to zero or more Access Groups.

**Viewer** — a member of an Access Group assigned as `viewers` for a Sensor. Can issue read-only commands for that Sensor.

**Admin** — a member of an Access Group assigned as `admins` for a Sensor. Can issue all commands for that Sensor. Implies Viewer access for the same Sensor.

**User** — any Telegram user whose chat_id appears in at least one Access Group. Users with no Access Group membership have no access to any Sensor.

**Telegram Group** — the single Telegram group where users send commands. The bot never replies with sensor data in the Group; all data replies are sent via DM.

**DM Registration** — the process by which a User activates private messaging with the bot. Triggered by clicking an HMAC-signed deep link sent by the bot in the Telegram Group. Required before the bot can send DM notifications or command replies to that User.

**HMAC Token** — a time-limited (24h TTL) signed token embedded in a Telegram start deep link (`t.me/botname?start=<token>`). Encodes the target chat_id and a timestamp. Verified on `/start` to ensure only the intended User completes DM Registration.

**Sensor Config** — YAML file mapping each Sensor name to its Topic, optional `json_field`, optional `interval`, and optional `viewers`/`admins` Access Group references. Global default interval: 300s. Sensors without `viewers` or `admins` are visible to nobody (fail-closed).

## Bot Commands

### User commands
| Command | Description |
|---|---|
| `/list` | List all sensors with current value and timestamp |
| `/get [name]` | Get value for one sensor (no arg = same as /list) |
| `/getAlarm [name]` | Show alarm threshold(s) |
| `/graph <name>` | Chart last 8h for a sensor |
| `/lastAlarm [name]` | Last alarm event (all sensors or specific one) |
| `/last5Alarm <name>` | Last 5 alarm events for a sensor |
| `/myid` | Show own Telegram user ID |
| `/help` | Show command list (admin-aware) |

### Admin-only commands
| Command | Description |
|---|---|
| `/setAlarm <name> <value>` | Set alarm threshold for a sensor |
| `/ackOff <name>` | Acknowledge offline alarm (suppresses repeats until sensor reconnects) |
| `/forgetSensor <name>` | Delete all data for a sensor (readings, alarms, threshold, silence state) |

**Daily Digest** — a scheduled silent message sent once per day at a configurable time (`digest_time` in credentials config, default `15:00`). Shows bot uptime and current readings for selected sensors. A sensor is included by setting `digest: true` in Sensor Config. Format: `🟢 live since 3d 4h` on first line, then one line per sensor as `name:value` with trailing ` *` if a threshold Alarm occurred in the last 24h. Offline sensors show `--` as value.

## Notification behaviour

- Alarm messages and Daily Digest sent via DM to each User, filtered to only the Sensors that User can see (Viewer or Admin).
- Command replies sent via DM, silently (`disable_notification=True`).
- Bot never sends sensor data in the Telegram Group. Group messages are limited to DM Registration prompts.
- If DM Registration is not yet completed for a User, the bot replies in the Telegram Group with a registration prompt and HMAC Token deep link, and no sensor data.
- Bot replies never quote or echo user input (`send_message` not `reply_text`).
- If a Sensor is not visible to the requesting User, the bot responds as if the Sensor does not exist.
