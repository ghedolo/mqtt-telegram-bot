#!/usr/bin/env python3
"""Split a monolithic sensors.yaml into the sensors.d/ directory layout.

Writes one file per device (`sensors.d/<device_key>.yaml`) and, if present,
the top-level `defaults:` block into `sensors.d/00-defaults.yaml` so it sorts
first. The bot reads every *.yaml / *.yml under sensors.d/ recursively and
merges them, so this layout is equivalent to the original single file.

Usage:
    python3 migrate_sensors.py [--yaml sensors.yaml] [--out sensors.d] [--force]
"""
import argparse
import os
import sys

import yaml


def _write(path: str, data: dict, force: bool) -> None:
    if os.path.exists(path) and not force:
        sys.exit(f"refuse to overwrite {path!r} (use --force)")
    with open(path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    print("wrote", path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Split sensors.yaml into sensors.d/")
    ap.add_argument("--yaml", default="sensors.yaml", help="source monolithic file")
    ap.add_argument("--out", default="sensors.d", help="target directory")
    ap.add_argument("--force", action="store_true", help="overwrite existing files")
    args = ap.parse_args()

    with open(args.yaml) as f:
        raw = yaml.safe_load(f) or {}

    devices = raw.get("devices") or {}
    if not devices:
        sys.exit(f"no devices found in {args.yaml!r}")

    os.makedirs(args.out, exist_ok=True)

    defaults = raw.get("defaults")
    if defaults:
        _write(os.path.join(args.out, "00-defaults.yaml"),
               {"defaults": defaults}, args.force)

    for dev_key, dv in devices.items():
        fname = dev_key.replace("/", "_").replace(os.sep, "_") + ".yaml"
        _write(os.path.join(args.out, fname),
               {"devices": {dev_key: dv}}, args.force)

    print(f"done: {len(devices)} device file(s) in {args.out}/")


if __name__ == "__main__":
    main()
