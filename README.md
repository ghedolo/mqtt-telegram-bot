# mqtt-telegram-bot

Telegram bot that monitors MQTT sensors, triggers alarms on threshold or offline events, and delivers daily digests — deployed via Docker.

---

## How it works

```
mqtt_client.py   →  subscribes to MQTT topics, calls on_reading() for each message
alarm_manager.py →  checks threshold and offline conditions, sends alarm notifications
telegram_bot.py  →  handles all Telegram commands and scheduled digest
db.py            →  SQLite storage: readings, thresholds, alarms, silence state
graph.py         →  matplotlib chart generation (multi-sensor, min/max markers)
config.py        →  loads sensors.yaml + credentials.yaml
main.py          →  entry point: wires components, runs periodic tasks
```

Data flow: MQTT messages → `on_reading()` → SQLite. `AlarmManager` polls for offline sensors and evaluates thresholds. Telegram commands query SQLite directly. A daily digest fires at the configured time.

---

## Prerequisites

- Docker v20.10+ (includes Compose as a built-in plugin)
- An MQTT broker (TLS or plain)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather)) and a group chat

---

## Installation

```bash
git clone https://github.com/ghedolo/mqtt-telegram-bot.git
cd mqtt-telegram-bot
cp credentials.yaml.example credentials.yaml
# edit credentials.yaml and sensors.yaml
docker compose up -d
```

---

## Configuration

### `sensors.yaml`

```yaml
sensors:
  SENSOR_NAME:
    topic: "mqtt/topic/path"
    info: "Human-readable label"   # optional, shown in /list
    unit: "°C"                     # optional
    json_field: "temperature"      # optional: if payload is JSON, extract this key as the value
    defaultAlarm: 30.0             # optional, seeds DB threshold on first run (admins can override with /setAlarm)
    interval: 300                  # expected publish interval in seconds
    viewers: [group_name]          # groups that can read this sensor
    admins: [ops]                  # groups that can administer this sensor (implies viewer)

defaults:
  interval: 300
  retention_days: 30
  alarm_threshold_repeat: 720     # seconds between repeated threshold alarms
  alarm_offline_repeat: 3600      # seconds between repeated offline alarms
  debug: 0                        # 0=silent 1=info 2=verbose
```

Sensors without `viewers` or `admins` are visible to nobody (fail-closed). See [`sensors.yaml.example`](sensors.yaml.example) for a full example.

### `credentials.yaml`

See [`credentials.yaml.example`](credentials.yaml.example).

Access Groups are defined at the top level of `credentials.yaml` and referenced by name in `sensors.yaml`.

---

## Bot commands

### User commands

| Command | Description |
|---|---|
| `/list` | All sensors — current value, timestamp, threshold |
| `/get [expr]` | Filtered sensors (no arg = personal digest subscriptions; see `/helpExpr`) |
| `/getAlarm [name]` | Show alarm threshold(s) |
| `/graph <expr> [Nh]` | Chart last N hours (default 8h, max 24h) |
| `/csv <expr> [Nh]` | Download readings as CSV |
| `/xlsx <expr> [Nh]` | Download readings as Excel (one sheet per sensor) |
| `/lastAlarm [name]` | Last alarm event |
| `/last5Alarm <name>` | Last 5 alarm events for a sensor |
| `/digest [expr on\|off]` | Manage daily digest subscriptions (no arg = show active) |
| `/helpExpr` | Sensor filter expression syntax |
| `/myid` | Your Telegram user ID |
| `/help` | Command list |

### Admin-only commands

| Command | Description |
|---|---|
| `/setAlarm <name> <value>` | Set alarm threshold |
| `/ackOff <name>` | Acknowledge offline alarm (suppresses repeats until sensor reconnects) |
| `/forgetSensor <name>` | Delete all data for a sensor |
| `/reloadConfig` | Reload `sensors.yaml` and `credentials.yaml` without restart |

### Sensor filter expressions (`/helpExpr`)

| Pattern | Matches |
|---|---|
| *(no arg)* | user's personal digest subscriptions |
| `*` | all sensors |
| `NAME` | exact name |
| `PREFIX*` | sensors starting with PREFIX |
| `*SUFFIX` | sensors ending with SUFFIX |
| `*SUB*` | sensors containing SUB |
| `A,B` or `A B` | comma- or space-separated patterns |

---

## Access control

Access is configured via named **Access Groups** in `credentials.yaml`. Each group is a list of Telegram `chat_id`s. Sensors reference group names under `viewers` and `admins`. Admin implies viewer for the same sensor. Users with no group assignment see no sensors.

**DM registration** is required before the bot can send private replies. When a user sends a command from the Telegram Group and has not yet activated DM, the bot sends a registration prompt with a signed button. Users can also send `/start` directly to the bot in DM.

## Notifications

- **Alarm messages** — sent via DM to all viewers/admins of the sensor.
- **Daily digest** — sent via DM to each user, showing only their subscribed sensors. Subscriptions start empty; manage with `/digest`.
- **Group daily message** — uptime only (`🟢 live since Xd Yh`), no sensor data.
- **Command replies** — sent via DM, silently (`disable_notification=True`).
- Bot replies never quote or echo user input.

---

## License

GPL-3.0. See [LICENSE](LICENSE).

---

## Author

ghedo (luca.ghedini@gmail.com) — 2026

Built with [Claude Code](https://claude.ai/claude-code) by Anthropic.

---

## Development effort

<!-- devstats:start -->
This project was built entirely through a conversation with Claude Code.
Numbers extracted from local session transcripts.

- **First message:** 2026-06-03
- **Last message:** 2026-06-09
- **Sessions:** 7 — 2030 messages (840 user + 1190 assistant)
- **Active conversation time:** ~535 min (~8h 55m)

*Active time: sum of consecutive gaps ≤ 5 min across all sessions. Longer gaps discarded.*

| Metric | Tokens |
|---|---:|
| Input (non-cache) | 2,889 |
| Output | 534,124 |
| Cache write | 2,164,572 |
| Cache read | 80,088,938 |
| **Total** | **~82 M** |

### Caveman mode

All 7 sessions ran with caveman mode active — a Claude Code skill that drops filler words, articles, and pleasantries from assistant responses while keeping full technical content. The assistant produced an average of **449 output tokens per message**. The saving is modest compared to prose-heavy projects because the dominant output here is code, which caveman leaves untouched.
<!-- devstats:end -->
