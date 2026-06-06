# CONTEXT.md — mqtt-telegram-bot

## Glossary

**Sensor** — a physical device that publishes temperature readings to the MQTT broker at a regular interval. Identified by a short name (e.g. `A`).

**Topic** — the MQTT topic path a Sensor publishes to. Defined per-sensor in config.

**Reading** — a single temperature value received from a Sensor, stored with a timestamp.

**Threshold** — an alarm temperature value set per-sensor by an Admin. A Reading above the Threshold triggers an Alarm.

**Alarm** — an active alert condition on a Sensor. Two types: `threshold` (temperature above Threshold) and `offline` (no Reading received for 3× the Sensor's publish interval). All alarm events are persisted in the `alarms` table.

**AckOff** — an Admin action that acknowledges an offline Alarm, suppressing repeat notifications until the Sensor comes back online (auto-clears on reconnect).

**Admin** — a Telegram user identified by chat_id, listed in config. Can issue all commands including set, ackOff, forgetSensor.

**User** — any member of the Telegram Group. Can issue read-only commands.

**Telegram Group** — the single group where all notifications and commands occur.

**Sensor Config** — YAML file mapping each Sensor name to its Topic, optional `json_field`, optional `interval`. Global default interval: 300s.

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

- Alarm messages sent with sound (standard Telegram notification).
- Command replies sent silently (`disable_notification=True`).
- Bot replies never quote or echo user input (`send_message` not `reply_text`).
