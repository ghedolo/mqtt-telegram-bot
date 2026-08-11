#!/usr/bin/env python3
"""Move a device's block out of a shared file and into a file of its own.

Config-only: no sensor name changes, so there is nothing to migrate in the DB
and no restart is needed for the data path — `sensors.d/` is read recursively
and merged, so which file a device lives in means nothing to the loader. It
means plenty to whoever maintains the tree, which is why rename_device.py
renames `OLD.yaml` to `NEW.yaml` and split_device.py puts a new device beside
the one it came from.

Use it when a device ended up in the wrong file — e.g. a split run before
split_device.py placed its output by file name, which left both halves in the
file named after one of them.

    python extract_device.py SM1_CDZ1 --dir sensors.d --dry-run

Usage:
    python extract_device.py KEY [--dir sensors.d] [--to PATH] [--dry-run]
"""
import argparse
import os
import sys

import yaml

from rename_device import find_device_file
from split_device import DEV_INDENT, _find_device_block


def extract(text: str, key: str) -> tuple[str, str]:
    """(source text without the device block, the block itself)."""
    lines = text.splitlines(keepends=True)
    start, end = _find_device_block(lines, key)
    block = "".join(lines[start:end])
    kept = "".join(lines[:start] + lines[end:])
    return kept, block


def main():
    ap = argparse.ArgumentParser(
        description="Move a device into its own file under sensors.d/"
    )
    ap.add_argument("key", help="device_key to move")
    ap.add_argument("--dir", default="sensors.d", help="sensors config directory")
    ap.add_argument("--to", default=None,
                    help="target file (default: KEY.yaml beside the source file)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = find_device_file(args.dir, args.key)
    if src is None:
        sys.exit(f"Device {args.key!r} not found under {args.dir}")

    with open(src) as f:
        text = f.read()
    declared = list((yaml.safe_load(text) or {}).get("devices") or {})
    if declared == [args.key]:
        sys.exit(f"{src} already holds only {args.key!r} — nothing to extract")

    target = args.to or os.path.join(
        os.path.dirname(src), args.key + os.path.splitext(src)[1]
    )
    if os.path.exists(target):
        sys.exit(f"Refusing: {target} already exists")

    kept, block = extract(text, args.key)
    new_text = "devices:\n" + block
    # A device block ends at its last content line, so the source can be left
    # with the blank line that separated it from its neighbour. Harmless to
    # YAML, untidy to read.
    kept = kept.rstrip("\n") + "\n"

    if args.dry_run:
        print(f"[dry-run] would rewrite {src}, dropping {args.key}:")
        for line in kept.splitlines():
            print(f"    {line}")
        print(f"[dry-run] would create {target}:")
        for line in new_text.splitlines():
            print(f"    {line}")
        print(f"\nDevices left in {src}: {[d for d in declared if d != args.key]}")
        return

    with open(src, "w") as f:
        f.write(kept)
    with open(target, "w") as f:
        f.write(new_text)
    print(f"Moved {args.key} from {src} to {target}")
    print(f"Devices left in {src}: {[d for d in declared if d != args.key]}")
    print("No DB change and no restart needed: no sensor name changed, and "
          "sensors.d/ is merged across files. A /reloadConfig is enough to "
          "confirm the tree still parses.")


if __name__ == "__main__":
    main()
