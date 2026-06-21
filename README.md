# mqtt-telegram-bot

Telegram bot that monitors MQTT sensors, triggers alarms on threshold or offline events, and delivers daily digests — deployed via Docker.

---

## How it works

```
mqtt_client.py   →  subscribes to MQTT topics, calls on_reading() for each message
alarm_manager.py →  checks threshold and offline conditions, sends alarm notifications
telegram_bot.py  →  handles all Telegram commands and scheduled digest
db.py            →  SQLite storage: readings, thresholds, alarms, silence state
graph.py         →  matplotlib chart generation (multi-sensor, min/max markers, glitch filtering, gap breaks)
config.py        →  loads sensors.yaml + credentials.yaml
main.py          →  entry point: wires components, runs periodic tasks
```

Data flow: MQTT messages → `on_reading()` → SQLite. Readings are rounded to at most 1 decimal place in `on_reading()` before storage and threshold checks, matching the precision of alarm thresholds. `AlarmManager` polls for offline sensors and evaluates thresholds. Telegram commands query SQLite directly. A daily digest fires at the configured time.

---

## Prerequisites

- **Docker rootless** — the only supported deployment mode (setup in [INSTALL.md](INSTALL.md)). A rootful daemon can coexist on the same host for other workloads.
- An MQTT broker (TLS or plain)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather)) and a group chat

---

## Installation

> The bot runs **only on rootless Docker**: even if the container is fully
> compromised, the attacker gets no root on the host. The container itself
> is hardened — unprivileged user (uid 999), read-only filesystem, all
> capabilities dropped, memory/CPU/pid limits, oversized MQTT payloads
> rejected. See [INSTALL.md](INSTALL.md) for the rootless setup and the
> data directory permission notes.

```bash
git clone https://github.com/ghedolo/mqtt-telegram-bot.git
cd mqtt-telegram-bot
cp credentials.yaml.example credentials.yaml
# edit credentials.yaml
# create sensors.yaml (see Configuration below)
docker compose up -d
```

---

## Configuration

### `sensors.yaml`

Sensors are grouped under **devices**. Each device maps to one MQTT topic (or per-field topics for devices that publish each value separately). The sensor name used in all commands is derived as `{device_key}_{field_key}`. Sensor names are **case-insensitive** in commands (e.g. `Office_Temp` matches `office_temp`); config parsing rejects two names that differ only by case.

```yaml
devices:
  DEVICE_KEY:
    topic: "mqtt/topic/path"       # shared topic for all fields (omit for per-field topics)
    info: "Human-readable label"   # shown in /list
    note: "Optional free text"     # not shown in bot, annotation only
    interval: 300                  # expected publish interval in seconds (default 300)
    viewers: [group_name]          # default viewer groups for all fields
    admins: [ops]                  # default admin groups for all fields (implies viewer)
    fields:
      FIELD_KEY:                   # sensor name = DEVICE_KEY_FIELD_KEY
        topic: "per/field/topic"   # required only if device has no shared topic
        json_path: "temperature"   # optional: JSON field to extract (dot notation for nested)
        unit: "°C"                 # optional
        defaultAlarmHigh: 30.0     # optional, seeds high threshold on first run
        defaultAlarmLow: 10.0      # optional, seeds low threshold on first run
        validMin: -20              # optional, plausible range floor (glitch filter)
        validMax: 80               # optional, plausible range ceiling (glitch filter)
        viewers: [other_group]     # optional: overrides device-level viewers (replaces, not merges)
        admins: [other_group]      # optional: overrides device-level admins (replaces, not merges)

defaults:
  interval: 300
  retention_days: 30
  alarm_threshold_repeat: 720     # seconds between repeated threshold alarms
  alarm_offline_repeat: 3600      # seconds between repeated offline alarms
```

Devices/fields without `viewers` or `admins` are visible to nobody (fail-closed).

Offline detection is per-device: one alarm fires when no message arrives on the device's topic(s) for `3 × interval`. For devices with per-field topics, the device is considered alive if any field topic received a message recently.

### Glitch filtering and graph gaps

All raw readings are always stored in the DB. The optional per-field `validMin`/`validMax` bounds filter only downstream:

- **Alarms** — a reading outside the range is stored but skipped for threshold checks, so a one-sample spike doesn't fire a false alarm.
- **Graphs** — out-of-range points are dropped from the line (no vertical spike). Each discarded reading is flagged with a tiny ▼ (above `validMax`, top edge) or ▲ (below `validMin`, bottom edge) at its timestamp, and the title shows `N fuori scala`. min/max stats use in-range values only.
- **Data gaps** — when the time between consecutive readings exceeds `interval × 2.5`, the graph line breaks instead of drawing a segment across the silence.
- `/csv` and `/xlsx` exports stay raw (unfiltered) for auditing a noisy sensor.

### `credentials.yaml`

See [`credentials.yaml.example`](credentials.yaml.example).

Access Groups are defined at the top level of `credentials.yaml` and referenced by name in `sensors.yaml`.

`superadmin` is a flat list of Telegram `chat_id`s with access to `/forgetSensor` and `/reloadConfig`. Independent of sensor-level groups.

---

## Bot commands

Only the **user commands** below are registered with Telegram via `set_my_commands`, so they appear in the client's `/` autocomplete menu. Admin and superadmin commands are intentionally left out of the menu but their handlers still work when typed.

> Tapping a command in Telegram's `/` menu sends it immediately, before you can type an argument (fixed client behaviour). For commands that need an argument (`/graph`, `/csv`, `/xlsx`, `/last5Alarm`), sending them bare replies with a `ForceReply` prompt — just reply with the argument and the command runs.

### User commands

| Command | Description |
|---|---|
| `/list` | All devices — one line per device with all visible fields and thresholds |
| `/get [expr]` | Filtered sensors (no arg = personal digest subscriptions; see `/helpExpr`) |
| `/getAlarm [name]` | Show alarm threshold(s) |
| `/graph <expr> [Nh]` | Chart last N hours (default 8h, max 24h) |
| `/csv <expr> [Nh]` | Download readings as CSV |
| `/xlsx <expr> [Nh]` | Download readings as Excel (one sheet per sensor) |
| `/last` | Last time any message arrived from MQTT (no content shown) |
| `/lastAlarms [expr] [Nh]` | All alarm events in the last N hours (default 8h, max 24h); no expr = digest subscriptions. 🔴 = alarm, 🟢 = recovery |
| `/last5Alarm <name>` | Last 5 alarm events for a sensor (🔴/🟢 markers) |
| `/digest [expr on\|off]` | Manage daily digest subscriptions (no arg = show active) |
| `/helpExpr` | Sensor filter expression syntax |
| `/myid` | Your Telegram user ID |
| `/help` | Command list |

### Admin-only commands

| Command | Description |
|---|---|
| `/setAlarm <name> <value>` | Set high alarm threshold (alarm if value >) |
| `/setAlarmLow <name> <value>` | Set low alarm threshold (alarm if value <) |
| `/clearAlarm <name>` | Clear high alarm threshold |
| `/clearAlarmLow <name>` | Clear low alarm threshold |
| `/ackOff <device>` | Acknowledge offline alarm for a device (suppresses repeats until it reconnects) |

### Superadmin-only commands

| Command | Description |
|---|---|
| `/forgetSensor <device>` | Archive all field readings for a device, clear alarm history and silence state |
| `/reloadConfig` | Reload `sensors.yaml` and `credentials.yaml` without restart |
| `/usersActivity` | Last interaction time per user (name, username, id, timestamp). Bot records this itself — Telegram does not expose user last-seen |

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

Three roles, all defined in `credentials.yaml`:

| Role | Definition | Permissions |
|---|---|---|
| **Viewer** | member of a group listed in a field's `viewers` | read-only commands on that sensor |
| **Admin** | member of a group listed in a field's `admins` | `/setAlarm`, `/ackOff` on that field/device; implies viewer |
| **Superadmin** | `superadmin:` flat list of `chat_id`s | `/forgetSensor`, `/reloadConfig` (global) |

Users with no group assignment see no sensors.

**DM registration** is required before the bot can send private replies. When a user sends a command from the Telegram Group and has not yet activated DM, the bot sends a registration prompt with a signed button. Users can also send `/start` directly to the bot in DM.

## Data management

Readings are stored in SQLite in two tables:

- `readings` — active window (default 30 days, set via `retention_days` in `sensors.yaml`)
- `readings_archive` — all readings older than the retention window, kept indefinitely

Every 24 hours the bot moves readings older than `retention_days` from `readings` to `readings_archive`. No data is ever deleted automatically.

`/forgetSensor <name>` moves all current readings for that sensor to the archive, deletes its alarm history and silence state, and preserves the alarm threshold.

## Notifications

- **Threshold alarms** — sent via DM to admins of the affected field (sensor).
- **Offline alarms** — one alarm per device, sent via DM to admins of fields for which the user has an active `/digest` subscription.
- **Daily digest** — sent via DM to each user, showing only their subscribed sensors grouped by device. Subscriptions start empty; manage with `/digest`. No daily message is posted to the group — the digest is per-user DM only.
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
- **Last message:** 2026-06-21
- **Sessions:** 14 — 4155 messages (1646 user + 2509 assistant)
- **Active conversation time:** ~1082 min (~18h 2m)

*Active time: sum of consecutive gaps ≤ 5 min across all sessions. Longer gaps discarded.*

| Metric | Tokens |
|---|---:|
| Input (non-cache) | 215,999 |
| Output | 1,153,377 |
| Cache write | 4,852,629 |
| Cache read | 175,085,127 |
| **Total** | **~181 M** |

### Caveman mode

All 14 sessions ran with caveman mode active — a Claude Code skill that drops filler words, articles, and pleasantries from assistant responses while keeping full technical content. The assistant produced an average of **460 output tokens per message**. The saving is modest compared to prose-heavy projects because the dominant output here is code, which caveman leaves untouched.
<!-- devstats:end -->
