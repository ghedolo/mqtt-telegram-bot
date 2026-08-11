#!/usr/bin/env python3
"""Split a device: move some of its fields out into a new device, in both the
sensors.d/ config tree and the SQLite DB.

The case this exists for is a device key that models two physical units at
once. `SM1_UTA1` carried the room probe (`T`, `H`) *and* the current meter
(`I`, `IF`); because offline detection is per-Device (the freshest message
across all of a device's topics), the meter publishing every few seconds kept
the whole device looking alive while the probe had been dead for days — no
OFFLINE alarm, and therefore no ONLINE one when it came back.

    python tools/split_device.py SM1_UTA1 SM1_CDZ1 --fields I,IF

The moved fields keep their field keys, so `SM1_UTA1_I` becomes `SM1_CDZ1_I`.
The new device inherits every device-level key of the old one (info, interval,
topic, viewers, admins, note, ...) verbatim — review `info` and `interval`
afterwards, since the whole point of the split is that the two halves publish
at different cadences.

Device-level DB rows (OFFLINE alarms and ackOff silence state, keyed by the
bare device key) are NOT moved: the old key survives the split and keeps its
own history.

What it touches, mirroring rename_device.py:
  - the `devices:` block holding OLD, in whatever file under sensors.d/ it
    lives in: the moved fields are cut and re-emitted under NEW
  - any other reference to a moved sensor name across the tree — in practice a
    blackout group's `fields:` list, which names sensors in full and would fail
    config load with "unknown field" if left behind
  - every sensor-keyed DB table (the list lives in rename_device.SENSOR_TABLES)

Usage:
    python tools/split_device.py OLD NEW --fields F1,F2 [--db data/sensors.db]
                           [--dir sensors.d] [--dry-run] [--skip-db] [--skip-yaml]

See docs/SPLIT_DEVICE.md for the two-step host/container procedure.
"""
import argparse
import os
import re
import sys
from typing import Optional

import yaml

# One source of truth for the sensor-keyed tables: rename_device's list is
# pinned against the real schema by tests/test_rename_device.py, so a table
# added to bot/db.py cannot fall out of a split either.
from rename_device import SENSOR_TABLES, _collect_yaml_files, find_device_file, update_db  # noqa: F401

# The config tree is hand-written with a fixed shape: `devices:` at column 0,
# device keys at 2, device attributes at 4, field keys at 6. rename_device.py
# relies on the same convention for the device-key line.
DEV_INDENT = 2
ATTR_INDENT = 4
FIELD_INDENT = 6


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank(line: str) -> bool:
    return not line.strip()


def load_device(file_path: str, key: str) -> dict:
    with open(file_path) as f:
        data = yaml.safe_load(f) or {}
    return (data.get("devices") or {}).get(key) or {}


def build_mapping(old: str, new: str, fields: list[str]) -> dict[str, str]:
    """Only the moved fields change name. The bare device key is deliberately
    absent: OLD still exists after a split, so remapping its device-level rows
    would hand the old device's offline history to the new one."""
    return {f"{old}_{fk}": f"{new}_{fk}" for fk in fields}


def _block_extent(lines: list[str], start: int, indent: int) -> int:
    """Index one past the last line belonging to the block opened at `start`.

    A block runs until the next non-blank line indented at or above `indent`
    (i.e. a sibling or an outdent). Trailing blank lines are left out, so they
    stay with whatever follows rather than being dragged into a moved block."""
    end = start + 1
    last_content = start + 1
    while end < len(lines):
        line = lines[end]
        if not _is_blank(line) and _indent(line) <= indent:
            break
        end += 1
        if not _is_blank(line):
            last_content = end
    return last_content


def _find_device_block(lines: list[str], key: str) -> tuple[int, int]:
    pattern = re.compile(rf"^ {{{DEV_INDENT}}}{re.escape(key)}:\s*$")
    for i, line in enumerate(lines):
        if pattern.match(line):
            return i, _block_extent(lines, i, DEV_INDENT)
    raise SystemExit(f"Could not locate device key line '{' ' * DEV_INDENT}{key}:'")


def _find_fields_block(lines: list[str], start: int, end: int) -> tuple[int, int]:
    pattern = re.compile(rf"^ {{{ATTR_INDENT}}}fields:\s*$")
    for i in range(start, end):
        if pattern.match(lines[i]):
            return i, _block_extent(lines, i, ATTR_INDENT)
    raise SystemExit("Device block has no 'fields:' line at 4-space indent")


def _field_entries(lines: list[str], start: int, end: int) -> dict[str, tuple[int, int]]:
    """field key → (first line, one past last line) for each field in the block."""
    pattern = re.compile(rf"^ {{{FIELD_INDENT}}}([A-Za-z0-9_]+):")
    entries: dict[str, tuple[int, int]] = {}
    for i in range(start, end):
        m = pattern.match(lines[i])
        if m:
            entries[m.group(1)] = (i, _block_extent(lines, i, FIELD_INDENT))
    return entries


def split_yaml_parts(text: str, old: str, new: str, fields: list[str]) -> tuple[str, str]:
    """(text of the source file with the fields removed, block defining `new`).

    Every line is carried across verbatim — comments and formatting in the moved
    fields survive the move. The caller decides where the new block lands: back
    into the same file, or into a file of its own."""
    lines = text.splitlines(keepends=True)
    dev_start, dev_end = _find_device_block(lines, old)
    f_start, f_end = _find_fields_block(lines, dev_start, dev_end)
    entries = _field_entries(lines, f_start + 1, f_end)

    missing = [fk for fk in fields if fk not in entries]
    if missing:
        raise SystemExit(f"Device {old!r} has no field(s) {missing} — nothing to move")
    remaining = [fk for fk in entries if fk not in fields]
    if not remaining:
        raise SystemExit(
            f"Moving every field of {old!r} is a rename, not a split — "
            f"use rename_device.py instead"
        )

    moved_idx = sorted(entries[fk] for fk in fields)
    moved_lines: list[str] = []
    for s, e in moved_idx:
        moved_lines.extend(lines[s:e])
    drop = {i for s, e in moved_idx for i in range(s, e)}

    # Device-level attributes = everything in the device block that is not the
    # fields subtree. Copied as-is so the new device starts life with the same
    # access lists and labels.
    attr_lines = [
        lines[i]
        for i in range(dev_start + 1, dev_end)
        if not (f_start <= i < f_end)
    ]

    kept_block = [lines[i] for i in range(dev_start, dev_end) if i not in drop]
    new_block = [f"{' ' * DEV_INDENT}{new}:\n", *attr_lines, f"{' ' * ATTR_INDENT}fields:\n", *moved_lines]

    # A device block is normally newline-terminated; if the file ended without
    # one, terminate it here so whatever follows starts on its own line.
    if kept_block and not kept_block[-1].endswith("\n"):
        kept_block[-1] += "\n"

    kept = "".join(lines[:dev_start] + kept_block + lines[dev_end:])
    return kept, "".join(new_block)


def split_yaml_text(text: str, old: str, new: str, fields: list[str]) -> str:
    """Both devices in one file: the new block is appended right after the old
    one, which is where it belongs when the file holds several devices."""
    kept, new_block = split_yaml_parts(text, old, new, fields)
    lines = kept.splitlines(keepends=True)
    _, dev_end = _find_device_block(lines, old)
    return "".join(lines[:dev_end] + ["\n"] + [new_block] + lines[dev_end:])


def new_file_path(dev_file: str, old: str, new: str) -> Optional[str]:
    """Where the new device's own file goes, when the tree is one file per
    device: `SM1_UTA1.yaml` → `SM1_CDZ1.yaml`, beside it. None when the source
    file is not named after the device it holds — then it holds several, and
    the new block belongs in it alongside them (rename_device.py reads the same
    signal to decide whether to rename the file)."""
    stem, ext = os.path.splitext(os.path.basename(dev_file))
    if stem != old:
        return None
    return os.path.join(os.path.dirname(dev_file), new + ext)


def update_yaml(file_path: str, old: str, new: str, fields: list[str], dry_run: bool,
                separate_file: Optional[str] = None) -> list[str]:
    """Move the fields out of `file_path`. With `separate_file`, the new device
    is written there as a file of its own; otherwise it is appended in place."""
    with open(file_path) as f:
        text = f.read()

    if separate_file is None:
        new_text = split_yaml_text(text, old, new, fields)
        if dry_run:
            print(f"[dry-run] would rewrite {file_path}, moving {','.join(fields)} to '{new}':")
            for line in new_text.splitlines():
                print(f"    {line}")
            return [file_path]
        with open(file_path, "w") as f:
            f.write(new_text)
        print(f"YAML: moved {','.join(fields)} from {old} to {new} in {file_path}")
        return [file_path]

    if os.path.exists(separate_file):
        sys.exit(f"Refusing: {separate_file} already exists")
    kept, new_block = split_yaml_parts(text, old, new, fields)
    new_text = "devices:\n" + new_block
    if dry_run:
        print(f"[dry-run] would rewrite {file_path}, dropping {','.join(fields)}:")
        for line in kept.splitlines():
            print(f"    {line}")
        print(f"[dry-run] would create {separate_file}:")
        for line in new_text.splitlines():
            print(f"    {line}")
        return [file_path, separate_file]
    with open(file_path, "w") as f:
        f.write(kept)
    with open(separate_file, "w") as f:
        f.write(new_text)
    print(f"YAML: dropped {','.join(fields)} from {old} in {file_path}")
    print(f"YAML: created {separate_file} with device {new}")
    return [file_path, separate_file]


def update_references(config_dir: str, dev_file: str, mapping: dict[str, str], dry_run: bool,
                      also_skip: Optional[str] = None) -> list[str]:
    """Rewrite full sensor names elsewhere in the tree — a blackout group's
    `fields:` list names sensors in full, and a stale name there is a hard
    config-load error ("unknown field"), i.e. a bot that will not start."""
    handled = {os.path.abspath(p) for p in (dev_file, also_skip) if p}
    touched: list[str] = []
    for fp in _collect_yaml_files(config_dir):
        if os.path.abspath(fp) in handled:
            continue  # the device blocks are handled by update_yaml
        with open(fp) as f:
            lines = f.readlines()

        changed: list[tuple[int, str, str]] = []
        out_lines = []
        for n, line in enumerate(lines, start=1):
            new_line = line
            for old_name, new_name in mapping.items():
                # word-bounded: SM1_UTA1_I must not match inside SM1_UTA1_IF
                new_line = re.sub(
                    rf"(?<![A-Za-z0-9_]){re.escape(old_name)}(?![A-Za-z0-9_])",
                    new_name, new_line,
                )
            if new_line != line:
                changed.append((n, line.rstrip("\n"), new_line.rstrip("\n")))
            out_lines.append(new_line)

        if not changed:
            continue
        # Show the lines, not just the file name: a blackout group's `fields:`
        # list is often a one-line flow sequence holding several sensors, only
        # some of which move, and "would update <file>" gives you nothing to
        # check that against.
        verb = "would update" if dry_run else "updated"
        print(f"{'[dry-run] ' if dry_run else 'YAML: '}{verb} sensor references in {fp}")
        for n, before, after in changed:
            print(f"    {n}: - {before.strip()}")
            print(f"    {n}: + {after.strip()}")
        touched.append(fp)
        if dry_run:
            continue
        with open(fp, "w") as f:
            f.writelines(out_lines)
    return touched


def main():
    ap = argparse.ArgumentParser(
        description="Split fields out of a device into a new device, in sensors.d/ and the DB"
    )
    ap.add_argument("old", help="device_key to split")
    ap.add_argument("new", help="device_key to create")
    ap.add_argument("--fields", required=True,
                    help="comma-separated field keys to move (e.g. I,IF)")
    ap.add_argument("--db", default="data/sensors.db")
    ap.add_argument("--dir", default="sensors.d", help="sensors config directory")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-db", action="store_true", help="do not touch the DB")
    ap.add_argument("--skip-yaml", action="store_true",
                    help="do not touch the sensors.d/ config")
    ap.add_argument("--new-file", metavar="PATH", default=None,
                    help="write the new device to this file instead of appending it "
                         "to the old one's (default: NEW.yaml beside it, when the "
                         "source file is named after the device it holds)")
    ap.add_argument("--same-file", action="store_true",
                    help="append the new device to the source file even when that "
                         "file is named after the old device")
    ap.add_argument("--config-done", action="store_true",
                    help="the sensors.d/ half is already done: migrate the DB from "
                         "--fields alone, without validating against the old device "
                         "(requires --skip-yaml)")
    args = ap.parse_args()

    if args.old == args.new:
        sys.exit("old and new device keys are identical")
    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    if not fields:
        sys.exit("--fields is empty")

    if args.config_done and not args.skip_yaml:
        sys.exit("--config-done only makes sense with --skip-yaml")

    if args.config_done:
        # Catching up the DB after the YAML half was already done. Every check
        # below reads the *old* device's field list from the config, which no
        # longer holds those fields — that is the state we are here to fix, so
        # there is nothing left to validate against. The mapping is taken from
        # --fields as given.
        declared = {}
        dev_file = None
    else:
        # Name the file, not just the directory: the tree is read recursively,
        # so a leftover backup copy or an editor's stray file two folders down
        # claims the key just as well as the real config, and "already exists
        # in sensors.d/" sends you looking in the one file you were about to
        # edit.
        clash = find_device_file(args.dir, args.new)
        if clash is not None:
            sys.exit(f"Device {args.new!r} is already defined in {clash}")
        dev_file = find_device_file(args.dir, args.old)
        if dev_file is None:
            sys.exit(f"Device {args.old!r} not found under {args.dir}")

        declared = load_device(dev_file, args.old).get("fields") or {}
        unknown = [fk for fk in fields if fk not in declared]
        if unknown:
            sys.exit(f"Device {args.old!r} has no field(s) {unknown}")
        if not set(declared) - set(fields):
            sys.exit(
                f"Moving every field of {args.old!r} is a rename, not a split — "
                f"use rename_device.py"
            )

    mapping = build_mapping(args.old, args.new, fields)
    where = "DB only, config already split" if args.config_done else f"in {dev_file}"
    print(f"Splitting {args.old} -> {args.new}  fields: {','.join(fields)}  ({where})")
    print("Mappings:")
    for fk in fields:
        # a Signal is never stored, so its rename is config-only — say so, or the
        # empty DB report below reads like the migration silently did nothing
        note = "  (signal: config only, no DB rows)" if (declared.get(fk) or {}).get("signal") else ""
        print(f"  {args.old}_{fk} -> {args.new}_{fk}{note}")

    if args.new_file and args.same_file:
        sys.exit("--new-file and --same-file contradict each other")
    # One file per device is the tree's own convention where it is followed:
    # a device living in a file named after it gets its half in a file named
    # after the new key. Anything else keeps both devices where they are.
    separate = args.new_file or (None if (args.same_file or dev_file is None)
                                 else new_file_path(dev_file, args.old, args.new))

    if not args.skip_db:
        update_db(args.db, mapping, args.dry_run)
    if not args.skip_yaml:
        touched = update_yaml(dev_file, args.old, args.new, fields, args.dry_run, separate)
        touched += update_references(args.dir, dev_file, mapping, args.dry_run, separate)
        # The device-file dry-run prints the whole rewritten file, which buries
        # everything after it — including the fact that a second file (the one
        # holding the blackout groups) is in scope at all. Restate it at the end.
        verb = "would change" if args.dry_run else "changed"
        print(f"\nConfig files {verb} ({len(touched)}):")
        for fp in touched:
            print(f"  {fp}")
    if not args.dry_run:
        print(
            "\nDone. Review 'info' and 'interval' on the new device, then restart the "
            "bot: the MQTT topic map is built at startup and still binds the moved "
            "topics to the old sensor names."
        )


if __name__ == "__main__":
    main()
