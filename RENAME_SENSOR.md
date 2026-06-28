# Renaming a device

`rename_device.py` renames a **device** (the `device_key` prefix of sensor
names) in both the `sensors.d/` config tree and the SQLite DB. Field keys are
kept: device `SM_UTA1` with fields `T`, `H` becomes `SM1_UTA1` with sensors
`SM1_UTA1_T`, `SM1_UTA1_H`.

The device may live in any `*.yaml` / `*.yml` file under `sensors.d/`
(subfolders included). The script locates the file holding the old key,
renames the key in place, and — if that file is named after the device
(`OLD.yaml`) — renames the file to `NEW.yaml` too.

It updates every sensor-keyed DB table (`readings`, `readings_archive`,
`thresholds`, `silenced`, `alarms`, `digest_subscriptions`) plus the
device-level rows used for offline alarms and silence state (keyed by the
bare `device_key`).

## Why two steps

In the deployed (rootless Docker) setup the two targets live in different
places and must be updated separately:

- **The DB** (`data/sensors.db`) is in a volume owned by the container's
  uid. The host user cannot write it → run the DB update **inside the
  container** (`--skip-yaml`).
- **`sensors.d/`** is mounted **read-only** into the container, but the
  host user owns the files → run the YAML update **on the host**
  (`--skip-db`).

The script is not baked into the image, so bind-mount it for the container
run (`-v ./rename_device.py:/app/rename_device.py`). No rebuild needed.

## Procedure

Always do the DB step first: the config still contains the old `device_key`,
which the script reads to discover the field list.

```bash
# 0. stop the bot (DB must not be in use)
docker compose down

# 1. DB update, inside a throwaway container (dry-run first)
docker compose run --rm -v ./rename_device.py:/app/rename_device.py bot \
  python3 rename_device.py OLD NEW --skip-yaml --dry-run
docker compose run --rm -v ./rename_device.py:/app/rename_device.py bot \
  python3 rename_device.py OLD NEW --skip-yaml

# 2. YAML update, on the host (dry-run first)
python3 rename_device.py OLD NEW --skip-db --dry-run
python3 rename_device.py OLD NEW --skip-db

# 3. restart
docker compose up -d
```

`docker compose run --rm bot ...` is a one-off container; it does not start
the main bot service, so the bot stays down until step 3.

## Flags

| Flag | Effect |
|---|---|
| `--dry-run` | Show what would change; DB writes are rolled back, config is not touched |
| `--skip-db` | Update `sensors.d/` only |
| `--skip-yaml` | Update the DB only |
| `--db PATH` | DB path (default `data/sensors.db`) |
| `--dir PATH` | Sensors config directory (default `sensors.d`) |

## Safety

- The script refuses to run if the new `device_key` already exists anywhere
  in `sensors.d/`, or if any target sensor name already has rows in a DB
  table.
- `--dry-run` performs the DB `UPDATE`s and rolls them back, so it opens the
  DB for writing — it will fail with `readonly database` if run as a user
  without write access (this is expected on the host; use the container).
- Take a quick backup first: copy `data/sensors.db` and `sensors.d/`.
- After restart, verify with `/get` and `/getAlarm`.
