#!/usr/bin/env python3
"""Rename a device (the device_key prefix of sensor names) in both the
sensors.d/ config tree and the SQLite DB.

The field keys are kept; only the device_key changes. For a device OLD
with fields T, H the sensor names OLD_T, OLD_H become NEW_T, NEW_H.
Device-level DB rows (offline alarms and silence state, keyed by the
bare device_key) are remapped too.

The device may live in any *.yaml / *.yml file under sensors.d/ (subfolders
included). The renamer locates the file holding OLD, renames the key in place,
and — if that file is named after the device (OLD.yaml) — renames the file too.

Usage:
    python rename_device.py OLD NEW [--db data/sensors.db] [--dir sensors.d] [--dry-run]
"""
import argparse
import os
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


def _collect_yaml_files(d: str) -> list[str]:
    files: list[str] = []
    for root, _dirs, names in os.walk(d):
        for n in names:
            if n.endswith((".yaml", ".yml")):
                files.append(os.path.join(root, n))
    return sorted(files)


def find_device_file(config_dir: str, key: str) -> str | None:
    """Path of the file under config_dir whose `devices:` block defines key."""
    for fp in _collect_yaml_files(config_dir):
        with open(fp) as f:
            data = yaml.safe_load(f) or {}
        if key in (data.get("devices") or {}):
            return fp
    return None


def load_device_fields(file_path: str, old: str) -> list[str]:
    with open(file_path) as f:
        data = yaml.safe_load(f) or {}
    dev = (data.get("devices") or {}).get(old) or {}
    if dev.get("fields"):
        return list(dev["fields"].keys())
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


def update_yaml(file_path: str, old: str, new: str, dry_run: bool):
    with open(file_path) as f:
        text = f.read()
    # device keys sit at exactly 2-space indent under `devices:`
    pattern = re.compile(rf"(?m)^(  ){re.escape(old)}:(\s*)$")
    if not pattern.search(text):
        sys.exit(f"Could not locate device key line '  {old}:' in {file_path}")
    new_text = pattern.sub(rf"\g<1>{new}:\g<2>", text, count=1)

    # if the file is named after the device, rename it too (OLD.yaml -> NEW.yaml)
    base = os.path.basename(file_path)
    stem, ext = os.path.splitext(base)
    new_path = file_path
    if stem == old:
        new_path = os.path.join(os.path.dirname(file_path), new + ext)

    if dry_run:
        print(f"[dry-run] would rename device key '  {old}:' -> '  {new}:' in {file_path}")
        if new_path != file_path:
            print(f"[dry-run] would rename file {file_path} -> {new_path}")
        return

    with open(file_path, "w") as f:
        f.write(new_text)
    if new_path != file_path:
        os.rename(file_path, new_path)
        print(f"YAML: renamed device key {old} -> {new} and file -> {new_path}")
    else:
        print(f"YAML: renamed device key {old} -> {new} in {file_path}")


def main():
    ap = argparse.ArgumentParser(
        description="Rename a device in the sensors.d/ config tree and the DB"
    )
    ap.add_argument("old", help="current device_key")
    ap.add_argument("new", help="new device_key")
    ap.add_argument("--db", default="data/sensors.db")
    ap.add_argument("--dir", default="sensors.d", help="sensors config directory")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-db", action="store_true", help="do not touch the DB")
    ap.add_argument("--skip-yaml", action="store_true",
                    help="do not touch the sensors.d/ config")
    args = ap.parse_args()

    if args.old == args.new:
        sys.exit("old and new device keys are identical")

    # reject new key already present anywhere in the config tree
    if find_device_file(args.dir, args.new) is not None:
        sys.exit(f"Device {args.new!r} already exists in {args.dir}")

    dev_file = find_device_file(args.dir, args.old)
    if dev_file is None:
        sys.exit(f"Device {args.old!r} not found under {args.dir}")

    fields = load_device_fields(dev_file, args.old)
    mapping = build_mapping(args.old, args.new, fields)
    print(f"Renaming device {args.old} -> {args.new}  (in {dev_file})")
    print("Mappings:")
    for o, n in mapping.items():
        print(f"  {o} -> {n}")

    if not args.skip_db:
        update_db(args.db, mapping, args.dry_run)
    if not args.skip_yaml:
        update_yaml(dev_file, args.old, args.new, args.dry_run)
    if not args.dry_run:
        print("Done. Restart the bot to pick up the new MQTT subscriptions / config.")


if __name__ == "__main__":
    main()
