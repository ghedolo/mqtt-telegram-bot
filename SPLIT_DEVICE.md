# Splitting a device

`split_device.py` moves some of a **device**'s fields out into a **new
device**, in both the `sensors.d/` config tree and the SQLite DB. Field keys
are kept: moving `I`, `IF` from `SM1_UTA1` to `SM1_CDZ1` turns the sensor
`SM1_UTA1_I` into `SM1_CDZ1_I`.

Use `rename_device.py` instead when the whole device changes key; this script
refuses to move *every* field, which is that case.

## Why you would

Offline detection is **per-Device**: a device counts as alive while any of its
topics is still publishing. A device key that models two physical units at once
therefore hides the death of the quieter one — `SM1_UTA1` carried both the room
probe (`T`, `H`, one message per interval) and the current meter (`I`, `IF`,
one every few seconds), so when the probe died the meter kept the device
looking alive: no OFFLINE alarm for days, and consequently no ONLINE message
when the probe came back (the recovery branch only fires for an alarm that was
actually raised).

Splitting them gives each unit its own offline check — and its own `interval`,
which is the setting that decides how fast an outage is noticed.

## What it touches

- **The device block** in whatever file under `sensors.d/` holds the old key:
  the moved fields are cut and re-emitted under the new key, immediately after
  the old block. Every line is carried across verbatim, comments included.
- **The new device's device-level keys** — `info`, `interval`, `topic`,
  `viewers`, `admins`, `note` — copied from the old device as-is. **Review
  them afterwards**: the two halves publish at different cadences, and the
  copied `interval` is the old, wrong, shared one.
- **Other references to the moved sensor names** across the tree — in practice
  a blackout group's `fields:` list, which names sensors in full. A stale name
  there is a hard config-load error (`unknown field`), i.e. a bot that will not
  start. The rewrite is word-bounded, so `SM1_UTA1_I` and `SM1_UTA1_IF` do not
  collide.
- **Every sensor-keyed DB table** (`readings`, `readings_archive`,
  `thresholds`, `silenced`, `alarms`, `digest_subscriptions`, `mutes`), for the
  moved fields only.

What it deliberately does **not** touch: the device-level rows keyed by the
bare device key (OFFLINE alarms, ackOff silence state). The old key survives a
split and keeps its own history.

A field marked `signal: true` is never stored, so its move is config-only — the
DB report will show no rows for it. That is expected, and the mapping printout
says so.

## Why two steps

Same as `RENAME_SENSOR.md`, for the same reason — in the deployed (rootless
Docker) setup the two targets live in different places:

- **The DB** (`data/sensors.db`) is in a volume owned by the container's uid.
  The host user cannot write it → run the DB update **inside the container**
  (`--skip-yaml`).
- **`sensors.d/`** is mounted **read-only** into the container, but the host
  user owns the files → run the YAML update **on the host** (`--skip-db`).

The script is not baked into the image, so bind-mount it for the container run.
No rebuild needed.

## Procedure

**The DB step must come first.** The script reads the old device's field list
from the config to validate `--fields`; once the YAML step has moved them, a
DB-only run is refused with `Device 'SM1_UTA1' has no field(s) ['I']`.

```bash
# 0. stop the bot (DB must not be in use), and back up first
docker compose down
cp data/sensors.db data/sensors.db.bak && cp -r sensors.d sensors.d.bak

# 1. DB update, inside a throwaway container (dry-run first)
docker compose run --rm -v ./split_device.py:/app/split_device.py \
  -v ./rename_device.py:/app/rename_device.py bot \
  python3 split_device.py SM1_UTA1 SM1_CDZ1 --fields I,IF --skip-yaml --dry-run
docker compose run --rm -v ./split_device.py:/app/split_device.py \
  -v ./rename_device.py:/app/rename_device.py bot \
  python3 split_device.py SM1_UTA1 SM1_CDZ1 --fields I,IF --skip-yaml

# 2. YAML update, on the host (dry-run prints the whole rewritten file)
python3 split_device.py SM1_UTA1 SM1_CDZ1 --fields I,IF --skip-db --dry-run
python3 split_device.py SM1_UTA1 SM1_CDZ1 --fields I,IF --skip-db

# 3. set the real intervals on both halves, by hand, in sensors.d/
#    (the new device inherited the old shared one)

# 4. restart — a reload is not enough: the MQTT topic map is built at startup
#    and still binds the moved topics to the old sensor names
docker compose up -d
```

`rename_device.py` is bind-mounted too: `split_device.py` imports its table
list and DB updater, so there is one definition of "every sensor-keyed table".

`docker compose run --rm bot ...` is a one-off container; it does not start the
main bot service, so the bot stays down until step 4.

## Flags

| Flag | Effect |
|---|---|
| `--fields F1,F2` | Field keys to move (required) |
| `--dry-run` | Show what would change; DB writes are rolled back, config is not touched (the rewritten file is printed) |
| `--skip-db` | Update `sensors.d/` only |
| `--skip-yaml` | Update the DB only |
| `--db PATH` | DB path (default `data/sensors.db`) |
| `--dir PATH` | Sensors config directory (default `sensors.d`) |

## Safety

- The script refuses to run if the new device key already exists anywhere in
  `sensors.d/`, if a named field is not on the old device, if the move would
  empty the old device, or if any target sensor name already has rows in a DB
  table.
- `--dry-run` performs the DB `UPDATE`s and rolls them back, so it opens the DB
  for writing — it will fail with `readonly database` on the host (expected;
  use the container).
- The YAML rewrite assumes the tree's standard shape: `devices:` at column 0,
  device keys at 2 spaces, attributes at 4, field keys at 6. It exits rather
  than guess if it cannot find the block.
- After restart, verify with `/get`, `/lastSeen`, `/listSignal` and
  `/lastAlarms <new key> 24h`.
