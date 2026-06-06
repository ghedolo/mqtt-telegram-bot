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

- Docker and Docker Compose
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
    defaultAlarm: 30.0             # optional, seeds DB threshold on first run
    digest: true                   # include in daily digest (default: false)
    interval: 300                  # expected publish interval in seconds

defaults:
  interval: 300
  retention_days: 30
  alarm_threshold_repeat: 720     # seconds between repeated threshold alarms
  alarm_offline_repeat: 3600      # seconds between repeated offline alarms
  debug: 0                        # 0=silent 1=info 2=verbose
```

### `credentials.yaml`

See [`credentials.yaml.example`](credentials.yaml.example).

---

## Bot commands

### User commands

| Command | Description |
|---|---|
| `/list` | All sensors — current value, timestamp, threshold |
| `/get [expr]` | Filtered sensors (no arg = digest sensors; see `/helpExpr`) |
| `/getAlarm [name]` | Show alarm threshold(s) |
| `/graph <expr> [Nh]` | Chart last N hours (default 8h, max 24h) |
| `/csv <expr> [Nh]` | Download readings as CSV |
| `/xlsx <expr> [Nh]` | Download readings as Excel (one sheet per sensor) |
| `/lastAlarm [name]` | Last alarm event |
| `/last5Alarm <name>` | Last 5 alarm events for a sensor |
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
| *(no arg)* | digest-flagged sensors only |
| `*` | all sensors |
| `NAME` | exact name |
| `PREFIX*` | sensors starting with PREFIX |
| `*SUFFIX` | sensors ending with SUFFIX |
| `*SUB*` | sensors containing SUB |
| `A,B` or `A B` | comma- or space-separated patterns |

---

## Notifications

- **Alarm messages** — sent with sound.
- **Command replies and digest** — sent silently (`disable_notification=True`).
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
- **Last message:** 2026-06-06
- **Sessions:** 5 — 1447 messages (612 user + 835 assistant)
- **Active conversation time:** ~376 min (~6h 16m)

*Active time: sum of consecutive gaps ≤ 5 min across all sessions. Longer gaps discarded.*

| Metric | Tokens |
|---|---:|
| Input (non-cache) | 2,234 |
| Output | 333,734 |
| Cache write | 1,224,731 |
| Cache read | 53,926,496 |
| **Total** | **~55 M** |
<!-- devstats:end -->
