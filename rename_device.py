#!/usr/bin/env python3
"""Rename a device (the device_key prefix of sensor names) in both
sensors.yaml and the SQLite DB.

The field keys are kept; only the device_key changes. For a device OLD
with fields T, H the sensor names OLD_T, OLD_H become NEW_T, NEW_H.
Device-level DB rows (offline alarms and silence state, keyed by the
bare device_key) are remapped too.

Usage:
    python rename_device.py OLD NEW [--db data/sensors.db] [--yaml sensors.yaml] [--dry-run]
"""
import argparse
import re
import sqlite3
import sys

import yaml

# every table that stores a sensor name in a `sensor` column
SENSOR_TABLES = [
    "readings",
    "readings_archive",
    "thresholds",
    "silenced",
    "alarms",
    "digest_subscriptions",
]


def load_device_fields(yaml_path: str, old: str) -> list[str]:
    with open(yaml_path) as f:
        data = yaml.safe_load(f) or {}
    devices = data.get("devices", {})
    if old not in devices:
        sys.exit(f"Device {old!r} not found in {yaml_path}")
    if (devices[old] or {}).get("fields"):
        return list(devices[old]["fields"].keys())
    return []


def build_mapping(old: str, new: str, fields: list[str]) -> dict[str, str]:
    # device-level key (offline alarms / silence) + every field sensor name
    mapping = {old: new}
    for fk in fields:
        mapping[f"{old}_{fk}"] = f"{new}_{fk}"
    return mapping


def update_db(db_path: str, mapping: dict[str, str], dry_run: bool):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        # guard: new names must not already exist in any table
        for table in SENSOR_TABLES:
            for new in mapping.values():
                row = con.execute(
                    f"SELECT 1 FROM {table} WHERE sensor=? LIMIT 1", (new,)
                ).fetchone()
                if row:
                    sys.exit(
                        f"Refusing: target sensor {new!r} already has rows in {table!r}"
                    )
        total = 0
        for table in SENSOR_TABLES:
            for old, new in mapping.items():
                cur = con.execute(
                    f"UPDATE {table} SET sensor=? WHERE sensor=?", (new, old)
                )
                if cur.rowcount:
                    print(f"  {table}: {old} -> {new}  ({cur.rowcount} rows)")
                    total += cur.rowcount
        if dry_run:
            con.rollback()
            print(f"[dry-run] would update {total} rows (rolled back)")
        else:
            con.commit()
            print(f"DB: updated {total} rows")
    finally:
        con.close()


def update_yaml(yaml_path: str, old: str, new: str, dry_run: bool):
    with open(yaml_path) as f:
        text = f.read()
    # device keys sit at exactly 2-space indent under `devices:`
    pattern = re.compile(rf"(?m)^(  ){re.escape(old)}:(\s*)$")
    if not pattern.search(text):
        sys.exit(f"Could not locate device key line '  {old}:' in {yaml_path}")
    new_text = pattern.sub(rf"\g<1>{new}:\g<2>", text, count=1)
    if dry_run:
        print(f"[dry-run] would rename device key '  {old}:' -> '  {new}:' in {yaml_path}")
        return
    with open(yaml_path, "w") as f:
        f.write(new_text)
    print(f"YAML: renamed device key {old} -> {new}")


def main():
    ap = argparse.ArgumentParser(description="Rename a device in sensors.yaml and the DB")
    ap.add_argument("old", help="current device_key")
    ap.add_argument("new", help="new device_key")
    ap.add_argument("--db", default="data/sensors.db")
    ap.add_argument("--yaml", default="sensors.yaml")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.old == args.new:
        sys.exit("old and new device keys are identical")

    # reject new key already present in yaml
    with open(args.yaml) as f:
        data = yaml.safe_load(f) or {}
    if args.new in (data.get("devices", {}) or {}):
        sys.exit(f"Device {args.new!r} already exists in {args.yaml}")

    fields = load_device_fields(args.yaml, args.old)
    mapping = build_mapping(args.old, args.new, fields)
    print(f"Renaming device {args.old} -> {args.new}")
    print("Mappings:")
    for o, n in mapping.items():
        print(f"  {o} -> {n}")

    update_db(args.db, mapping, args.dry_run)
    update_yaml(args.yaml, args.old, args.new, args.dry_run)
    if not args.dry_run:
        print("Done. Restart the bot to pick up the new MQTT subscriptions / config.")


if __name__ == "__main__":
    main()
