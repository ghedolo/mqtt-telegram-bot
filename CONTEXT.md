# CONTEXT.md — mqtt-telegram-bot

## Glossary

**Sensor** — a physical device that publishes temperature readings to the MQTT broker at a regular interval. Identified by a short name (e.g. `A`).

**Topic** — the MQTT topic path a Sensor publishes to. Defined per-sensor in config.

**Reading** — a single temperature value received from a Sensor, stored with a timestamp.

**Threshold** — an alarm temperature value set per-sensor by an Admin. A Reading above the Threshold triggers an Alarm.

**Alarm** — an active alert condition on a Sensor. Two types: `threshold` (temperature above Threshold) and `offline` (no Reading received for 3× the Sensor's publish interval).

**Silence** — an Admin action that suppresses Alarm notifications for an offline Sensor until it comes back online.

**Admin** — a Telegram user identified by chat_id, listed in config. Can issue all commands including `set` and silence.

**User** — any member of the Telegram Group. Can issue read-only commands (`get`, `graph`).

**Telegram Group** — the single group where all notifications and commands occur.

**Sensor Config** — YAML file mapping each Sensor name to its Topic, optional `json_field`, optional `interval`. Global default interval: 300s.
