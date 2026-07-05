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
config.py        →  loads sensors.d/ (recursive) + credentials.yaml
main.py          →  entry point: wires components, runs periodic tasks
```

Data flow: MQTT messages → `on_reading()` → SQLite. Readings are rounded in `on_reading()` to the field's configured `decimals` (default 1) before storage and threshold checks, matching the precision of alarm thresholds. `AlarmManager` polls for offline sensors and evaluates thresholds. Telegram commands query SQLite directly. A daily digest fires at the configured time.

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
# create the sensors.d/ config dir (see Configuration below)
docker compose up -d
```

---

## Configuration

### `sensors.d/`

Sensor config lives in the **`sensors.d/` directory**, not a single file. Every `*.yaml` / `*.yml` file under it is read **recursively** (subfolders allowed) and merged at startup — split devices however you like (e.g. one file per device or per building). A duplicate device key across files is a hard error. Convert an old monolithic `sensors.yaml` with `python3 migrate_sensors.py`.

The shared **`defaults:` block lives only in `00-defaults.yaml`** (which may also carry `devices:`). Every other file must contain **nothing but `devices:`** — any stray top-level key is a hard error, so there is no ambiguity about where defaults come from.

Sensors are grouped under **devices**. Each device maps to one MQTT topic (or per-field topics for devices that publish each value separately). The sensor name used in all commands is derived as `{device_key}_{field_key}`. Sensor names are **case-insensitive** in commands (e.g. `Office_Temp` matches `office_temp`); config parsing rejects two names that differ only by case.

A device file (`devices:` only); the `defaults:` block at the bottom is valid only inside `00-defaults.yaml`:

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
        decimals: 1                # optional, decimal places kept (0-5, default 1)
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

`decimals` (0-5, default 1) sets how many decimal places each reading is rounded to for storage and shown with everywhere — `/get`, `/list`, alarm messages, `/setAlarm` input, and graph stats. Out-of-range values are rejected at startup.

Offline detection is per-device: one alarm fires when no message arrives on the device's topic(s) for `3 × interval`. For devices with per-field topics, the device is considered alive if any field topic received a message recently.

Threshold alarms are evaluated on every incoming reading. When a value first crosses a field's high threshold (`/setAlarm`) or low threshold (`/setAlarmLow`), a `🔴` alarm is sent. While the value stays out of range the alarm repeats, but no more often than `alarm_threshold_repeat` seconds (default 720). The repeat is not a fixed timer: it is checked only when a new reading arrives, so it fires on the first reading after that interval has elapsed — a rarely-reporting sensor repeats later than the nominal period. When the value returns within range a single `🟢` recovery message is sent and the alarm resets. Offline alarms repeat the same way, gated by `alarm_offline_repeat` (default 3600), and auto-clear when the device reports again.

### Blackout detection

A **blackout** is a derived alarm inferred from current (amps) Fields. The monitoring stack (sensors, MQTT, the bot) runs on UPS while the measured loads (e.g. air-handling units) run on mains, so a mains outage shows up as every watched current dropping to near-zero while the meters keep reporting. A live-but-idle load still draws a baseline current, so a near-zero reading means *unpowered*, not idle.

Configure blackout groups under `blackouts:` in `00-defaults.yaml` (the only file allowed non-`devices` keys), as a map of group id → rule:

```yaml
blackouts:
  R2:
    info: "Blackout R2 — CDZ senza corrente"
    fields: [SM1_UTA1_I, SM1_UTA2_I]   # canonical sensor names to watch
    below: 0.5                          # A; all fields must read under this
    for_seconds: 10                     # sustained duration before raising (0 = on first dark reading)
    stale_after: 15                     # a reading older than `stale_after` seconds does not count; keep it ≥ meter publish interval
    repeat_seconds: 3600                # re-notify interval (default: alarm_offline_repeat)
```

Unknown field names are rejected at startup. Each field is classified from its latest reading: **DARK** (fresh — newer than `stale_after` — and below `below`), **LIT** (fresh and at/above `below` → power present), or **UNKNOWN** (stale or missing → no evidence). A blackout alarm (`⚡`) is raised when **every** field is DARK, sustained for `for_seconds`, and repeats no more often than `repeat_seconds`.

The end-of-blackout message (`🔌`) is sent **only on positive proof** — when at least one field becomes LIT. A field going UNKNOWN (its meter stopped publishing) does **not** end the blackout: without a fresh reading the bot can't claim power is back, so it holds the alarm and stays silent, while that field's own device **offline** alarm reports the silence. This avoids a false "power restored" when, during a real outage, one current meter dies but another still reads zero.

The full state model (per-field DARK/LIT/UNKNOWN inputs plus the group POWERED→SUSPECTED→OUTAGE machine, with a diagram) is in [docs/blackout-states.md](docs/blackout-states.md).

`for_seconds` (the sustain window) and `stale_after` (the freshness window) are **independent**: set `for_seconds` as low as you like to catch brief outages, but keep `stale_after ≥ the meter's real publish interval`, or fresh readings would be wrongly discarded and the blackout never raised. Evaluation is **event-driven** — the group is re-checked on every incoming current reading, so detection latency is roughly the meter's publish cadence. That cadence is also the hard floor on resolution: a blackout shorter than the interval between two published readings cannot be observed.

#### Understanding `stale_after`

`stale_after` does **not** change *how fast* a blackout is detected (that is `for_seconds` plus the meter cadence). It controls **how old a reading may be to still count as evidence**. A field counts as "dark" only when its latest reading is both below `below` **and** no older than `stale_after`; an older reading is ignored and the field is treated as not-dark.

It exists for a specific failure: if a meter *stops publishing* while its last value happened to be near-zero, without a freshness limit that stale zero would look like a permanent blackout forever. `stale_after` says "stale data doesn't count". It therefore plays a **double role** — the blackout also auto-clears when a field *goes stale* (not only when it rises above `below`), so `stale_after` is effectively the "assume power is back after this much silence" timeout.

Pick it at roughly **2–4× the real publish cadence**:

| `stale_after` vs cadence | effect |
|---|---|
| **too low** (< cadence) | between two publishes the reading ages past the window → stale → never dark → **blackout never raised** (this was the original bug). A single late/dropped message drops the condition. |
| **right** (~2–4×) | tolerates a couple of missed/late messages; if the meter goes silent for longer, the blackout is treated as ended. |
| **too high** (e.g. minutes) | a meter that *dies* mid-blackout keeps the alarm falsely "active" for that long; recovery is slow. |

Example — cadence 5 s, `stale_after: 15`:

- meter publishes `0` at t=0, 5, 10 … → each reading is ≤ 15 s old → dark holds.
- the t=10 message is dropped, but the t=5 one is still there → at t=12 its age is 7 s ≤ 15 → **still dark** (a missed message is tolerated).
- the meter goes fully silent after t=5 → at t=21 the last reading is 16 s old > 15 → **stale** → the blackout is auto-cleared.

Notification is **opt-in**: the group id is a subscribable pseudo-entity — `/digest R2 on`. It carries no reading, so it never appears as a value row in `/get` or the daily digest; it only serves as the notification flag. Any **viewer** of at least one watched field may subscribe (more permissive than offline alarms, which are admin-gated, because subscription is an explicit opt-in). Blackout groups are listed at the bottom of `/list` (with a 🔔/🔕 subscription marker) so users can discover them. See [ADR-0007](docs/adr/0007-blackout-detection-from-current.md).

### Glitch filtering and graph gaps

All raw readings are always stored in the DB. The optional per-field `validMin`/`validMax` bounds filter only downstream:

- **Alarms** — a reading outside the range is stored but skipped for threshold checks, so a one-sample spike doesn't fire a false alarm.
- **Graphs** — out-of-range points are dropped from the line (no vertical spike). Each discarded reading is flagged with a tiny ▼ (above `validMax`, top edge) or ▲ (below `validMin`, bottom edge) at its timestamp, and the title shows `N fuori scala`. min/max stats use in-range values only.
- **Data gaps** — when the time between consecutive readings exceeds `interval × 2.5`, the graph line breaks instead of drawing a segment across the silence.
- `/csv` and `/xlsx` exports stay raw (unfiltered) for auditing a noisy sensor.

### `credentials.yaml`

See [`credentials.yaml.example`](credentials.yaml.example).

Access Groups are defined at the top level of `credentials.yaml` and referenced by name in `sensors.d/`.

`superadmin` is a flat list of Telegram `chat_id`s with access to `/forgetSensor` and `/reloadConfig`. Independent of sensor-level groups.

### Changing configuration — what each change costs

Every value in the YAML has a different cost to change. Three levels, cheapest to most expensive:

- **Reload** — run `/reloadConfig` (superadmin); no downtime.
- **Restart** — `docker compose restart` (or `down`/`up`); a few seconds of downtime.
- **DB migration** — the data in `data/sensors.db` is keyed by name, so a rename orphans history unless the DB is migrated too (see [RENAME_SENSOR.md](RENAME_SENSOR.md)).

Why some changes still need a restart: MQTT topic subscriptions are set up **once at startup**, so anything that changes what/where the bot subscribes needs a restart. Everything else the `AlarmManager` uses (alarm-repeat intervals, blackout rules) is refreshed live by `/reloadConfig`.

| Change | Cost | Notes |
|---|---|---|
| `info`, `note` (labels) | **Reload** | Cosmetic; shown in `/list`. |
| `unit`, `decimals` | **Reload** | `decimals` affects only *new* readings; old stored values keep their precision. |
| `validMin` / `validMax` | **Reload** | Glitch filter, applied to new readings. |
| `viewers` / `admins` | **Reload** | Access changes take effect immediately. |
| `defaultAlarmHigh` / `defaultAlarmLow` | **Reload** | Only *seeds* a threshold when none is set yet; to re-seed, clear the threshold first. |
| `interval` (device/field) | **Reload** | Offline detection (`3×interval`) picks it up live. |
| Access Groups / `superadmin` (credentials) | **Reload** | Membership and admin lists are live. |
| `retention_days` | **Reload** | Read by the daily archive task. |
| Renaming a **config file** (e.g. `SM1.yaml` → `foo.yaml`) | **Reload** | Files are merged by *content*, not filename — no effect. Exception: renaming **to/from `00-defaults.yaml`** changes which file may carry `defaults:`/`blackouts:`. |
| `topic` (same sensor name) | **Restart** | MQTT re-subscribes only at startup. |
| Adding a new device / field / sensor | **Restart** | Reload makes it visible in commands, but no data flows until MQTT subscribes at restart. |
| Removing a device / field | **Restart** | Reload hides it; the old MQTT subscription lingers until restart (harmless). Old DB rows remain — archive them with `/forgetSensor`. |
| `alarm_threshold_repeat` / `alarm_offline_repeat` | **Reload** | Pushed into the running `AlarmManager` on reload. |
| Blackout tuning: `below`, `for_seconds`, `stale_after`, `repeat_seconds`, `info` | **Reload** | Applied to the running `AlarmManager` immediately. |
| Add / remove a **blackout group** | **Reload** | New group active at once. Removing one leaves its old alarm history and `/digest` subscriptions in the DB (harmless, just ignored). |
| Add / remove a **current field** in a group (`fields:`) | **Reload** *(existing sensor)* / **Restart** *(brand-new sensor)* | All listed fields must be near-zero *at the same time* to trigger, so adding one tightens the condition and removing one loosens it. A field that is a brand-new sensor needs a restart first (it has no readings until MQTT subscribes). |
| Rename a **blackout group id** (`R2` → `R2b`) | **Reload** *(+ re-subscribe)* | The new id works after reload, but the id is the key for `/digest` subscriptions and past alarm rows: users subscribed to the old id are silently dropped and **must re-subscribe** (`/digest R2b on`). To preserve them, migrate the old id to the new one in the `digest_subscriptions` (and `alarms`) tables. |
| MQTT host/port/user/pass/tls, Telegram token/`group_id`, `poll_interval`, `digest_time`, `silent_start`, `debug` | **Restart** | Read only at startup. |
| Renaming a **device key** (`SM_UTA1` → `SM1_UTA1`) | **Restart + DB migration** | Changes every derived sensor name. Use `rename_device.py` (config + DB); see [RENAME_SENSOR.md](RENAME_SENSOR.md). Without migration, history/thresholds/subscriptions/mutes for the old name are orphaned and the new name starts empty. |
| Renaming a **field key** (`T` → `Temp`) | **Restart + DB migration** | Same orphaning — the sensor name (`device_field`) changes. No dedicated script; migrate the DB by hand or accept the history loss. |

The safe order for a rename is always: stop the bot → migrate the DB → edit the YAML → restart (the DB step reads the *old* key from config, so migrate before editing the YAML — or follow the two-step procedure in RENAME_SENSOR.md for the read-only-mounted Docker setup).

---

## Bot commands

Only the **user commands** below are registered with Telegram via `set_my_commands`, so they appear in the client's `/` autocomplete menu. Admin and superadmin commands are intentionally left out of the menu but their handlers still work when typed.

> Tapping a command in Telegram's `/` menu sends it immediately, before you can type an argument (fixed client behaviour). For commands that need an argument (`/graph`, `/csv`, `/xlsx`, `/last5Alarm`), sending them bare replies with a `ForceReply` prompt — reply with the argument and the command runs. Telegram Web ignores ForceReply focus, so there you can just send the argument as a normal message within 30s of the prompt. The prompt message is deleted once you answer, so its reply box clears on all your devices.

### User commands

| Command | Description |
|---|---|
| `/list` | All devices — one line per device with all visible fields and thresholds; also lists subscribable blackout groups with your subscription state (🔔/🔕) |
| `/get [expr] [-s\|-f]` | Filtered sensors (no arg = personal digest subscriptions; see `/exprSyntax`). Sort: `-s` by name, `-f` by field (default) |
| `/getAlarm [name]` | Show alarm threshold(s) |
| `/graph <expr> [Nh]` | Chart last N hours (default 8h, max 24h; 72h for admins) |
| `/csv <expr> [Nh]` | Download readings as CSV (default 8h, max 24h; 72h for admins) |
| `/xlsx <expr> [Nh]` | Download readings as Excel, one sheet per sensor (default 8h, max 24h; 72h for admins) |
| `/last` | Last time any message arrived from MQTT (no content shown) |
| `/lastAlarms [expr] [Nh]` | All alarm events in the last N hours (default 8h, max 24h); no expr = digest subscriptions. 🔴 = alarm, 🟢 = recovery |
| `/last5Alarm <name>` | Last 5 alarm events for a sensor (🔴/🟢 markers) |
| `/digest [expr on\|off]` | Manage daily digest subscriptions; also blackout group ids (no arg = show active) |
| `/silent [expr [Nh]]` | Mute your own threshold-alarm DMs per sensor. No arg = list active mutes; `expr Nh` = mute for N hours (1–24); `expr` alone = unmute. Temporary and per-user; does not affect offline alarms (see `/ackOff`) |
| `/exprSyntax` | Sensor filter expression syntax |
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
| `/reloadConfig` | Reload `sensors.d/` and `credentials.yaml` without restart |
| `/usersActivity` | Last interaction time per user (name, username, id, timestamp). Bot records this itself — Telegram does not expose user last-seen |
| `/dbStats` | DB file size on disk, space reclaimable by VACUUM, and row counts + time span for `readings` and `readings_archive` |

### Sensor filter expressions (`/exprSyntax`)

| Pattern | Matches |
|---|---|
| *(no arg)* | user's personal digest subscriptions |
| `*` | all sensors |
| `NAME` | exact name |
| `PREFIX*` | sensors starting with PREFIX |
| `*SUFFIX` | sensors ending with SUFFIX |
| `*SUB*` | sensors containing SUB |
| `A,B` or `A B` | comma- or space-separated patterns |

Sort (only `/get`): default groups by field (all `_T`, then `_H`, …). `-s` sorts by sensor name instead; `-f` is the explicit field default. Example: `/get * -s`.

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

For a non-technical, step-by-step onboarding walkthrough to hand to end users: [docs/USER-GUIDE.md](docs/USER-GUIDE.md) (English) · [docs/GUIDA-UTENTE.md](docs/GUIDA-UTENTE.md) (Italiano).

## Data management

Readings are stored in SQLite in two tables:

- `readings` — active window (default 30 days, set via `retention_days` in `sensors.d/`)
- `readings_archive` — all readings older than the retention window, kept indefinitely

Every 24 hours the bot moves readings older than `retention_days` from `readings` to `readings_archive`. No data is ever deleted automatically.

`/forgetSensor <name>` moves all current readings for that sensor to the archive, deletes its alarm history and silence state, and preserves the alarm threshold.

## Notifications

- **Threshold alarms** — sent via DM to admins of the affected field (sensor).
- **Offline alarms** — one alarm per device, sent via DM to admins of fields for which the user has an active `/digest` subscription.
- **Blackout alarms** — one alarm per blackout group when all its current fields read near-zero for a sustained duration; sent via DM to viewers of a watched field who subscribed to the group id via `/digest`. Auto-clears with an end-of-blackout message. See _Blackout detection_ above.
- **Daily digest** — sent via DM to each user, showing only their subscribed sensors as the same monospace `Sensor | value | min ago` table as `/get` with no args. Subscriptions start empty; manage with `/digest`. No daily message is posted to the group — the digest is per-user DM only.
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
- **Last message:** 2026-07-05
- **Sessions:** 16 — 6027 messages (2336 user + 3691 assistant)
- **Active conversation time:** ~1505 min (~25h 5m)

*Active time: sum of consecutive gaps ≤ 5 min across all sessions. Longer gaps discarded.*

| Metric | Tokens |
|---|---:|
| Input (non-cache) | 422,868 |
| Output | 1,926,632 |
| Cache write | 8,975,887 |
| Cache read | 357,695,195 |
| **Total** | **~369 M** |

### Caveman mode

All 16 sessions ran with caveman mode active — a Claude Code skill that drops filler words, articles, and pleasantries from assistant responses while keeping full technical content. The assistant produced an average of **522 output tokens per message**. The saving is modest compared to prose-heavy projects because the dominant output here is code, which caveman leaves untouched.
<!-- devstats:end -->
