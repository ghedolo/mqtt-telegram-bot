#!/usr/bin/env python3
"""Is a low current reading a real load, or the meter's zero-current floor?

A blackout is inferred from current dropping "near zero", but the messages show
values such as `SM1_CDZ2_IF=0.4` during (and after) the outage. Two readings of
that number are possible and they demand different actions:

  FLOOR  — the meter/CT never reports 0.0: with no current flowing it settles on
           a small constant offset. 0.4 A then means *unpowered*, the detection
           is right, and `below` only has to sit above that offset.
  LOAD   — the meter can and does report 0.0, so 0.4 A is genuine current: a
           residual circuit still fed during the outage, or a load that is
           simply idle. The group's `below` is then measuring the wrong thing
           for that field.

The test that separates them is the recorded history of the field:

  1. does the field EVER reach 0.0 (or anything below the low cluster)? If yes,
     the meter can express zero, so a persistent 0.4 is current, not offset.
  2. is the low tail a tight cluster at a non-zero constant, separated from the
     working values by an empty gap? That shape is an offset, not a load.
  3. does that same low value occur OUTSIDE every recorded blackout window? A
     value seen while power is provably present cannot be the outage signature.

For each watched field the script prints those three answers plus a verdict, and
compares the value distribution inside recorded blackout windows against the
rest of the history.

Signals (`signal: true`) are never stored, so a group watching e.g. `SM1_CDZ1_IF`
has no history to read: the script falls back to the stored current Fields of the
same Device (the slow `_I` sibling), and says so. The floor is a property of the
meter, so the slow field answers the question for the fast one.

Stored values are rounded to the Field's configured `decimals` (1 by default),
so anything under 0.05 A is stored as 0.0 — the floor can only be resolved to
that step.

READ-ONLY: the DB is opened with mode=ro and never written; the config is only
read. Nothing here changes bot state.

Run inside the deploy (cwd = /app, so sensors.d/, credentials.yaml and
data/sensors.db are all present):

  docker compose exec -T bot python - < tools/floor_diag.py
  docker compose exec -T bot python - --days 30 < tools/floor_diag.py
  docker compose exec -T bot python - --field SM1_CDZ2_I --days 90 < tools/floor_diag.py

Feeding the script on stdin avoids a bind-mount.

Args:
  --days N       history span (0 = all, the default)
  --field NAME   analyse this stored Field too (repeatable); accepts a glob
  --group ID     restrict to one blackout group
  --low A        low-tail cut-off in amps (default: the group's `below`)
"""
import argparse
import fnmatch
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone

from bot import config
from bot import db as botdb


# ---------------------------------------------------------------- helpers

def ro_conn(path=None):
    """Open the readings DB strictly read-only — cannot create or mutate it."""
    uri = f"file:{path or botdb.DB_PATH}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def has_table(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def read_history(con, sensor: str, since_ts: int):
    """Oldest-first (ts, value) for a Field, across `readings` and
    `readings_archive` — the floor is a long-run property, and most of the
    history worth measuring is older than retention."""
    q = "SELECT ts, value FROM readings WHERE sensor=? AND ts>=?"
    args = [sensor, since_ts]
    if has_table(con, "readings_archive"):
        q += (" UNION ALL SELECT ts, value FROM readings_archive "
              "WHERE sensor=? AND ts>=?")
        args += [sensor, since_ts]
    q += " ORDER BY ts ASC"
    return con.execute(q, args).fetchall()


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def fmt_dur(s):
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def load_cfg():
    return config.load("sensors.d", "credentials.yaml")


def stored_proxies(cfg, name):
    """Stored Fields that can stand in for `name` when it has no history.

    A Signal keeps no Readings, so its own floor is unmeasurable from the DB.
    The floor belongs to the meter, not to the sampling rate, so the same
    Device's stored current Fields answer the same question."""
    dev = cfg.device_of(name)
    if not dev:
        return []
    out = []
    for sname, sc in cfg.sensors.items():
        if sc.device_key == dev and (sc.unit or "").strip().upper() == "A":
            out.append(sname)
    return out


def outage_windows(con, group_id):
    """Recorded blackout windows for a group, as (start_ts, end_ts) pairs.

    Built from the alarms table: a BLACKOUT row opens a window, the next
    BLACKOUT_END closes it. A window still open at the end of history is closed
    with None, and treated as running to `now`."""
    rows = con.execute(
        "SELECT kind, ts FROM alarms WHERE sensor=? AND kind IN "
        "('BLACKOUT','BLACKOUT_END') ORDER BY ts ASC",
        (group_id,),
    ).fetchall()
    wins, start = [], None
    for r in rows:
        if r["kind"] == "BLACKOUT":
            if start is None:
                start = r["ts"]
        else:
            if start is not None:
                wins.append((start, r["ts"]))
                start = None
    if start is not None:
        wins.append((start, None))
    return wins


def in_any(ts, wins, now):
    for a, b in wins:
        if a <= ts <= (b if b is not None else now):
            return True
    return False


def biggest_gap(values, upto):
    """Largest empty interval between consecutive distinct values below `upto`.

    A meter offset shows up as a tight low cluster with nothing between it and
    the working range; a genuine load fills that space."""
    ds = sorted({v for v in values if v <= upto})
    if len(ds) < 2:
        return None
    gap, lo, hi = 0.0, None, None
    for a, b in zip(ds, ds[1:]):
        if b - a > gap:
            gap, lo, hi = b - a, a, b
    return gap, lo, hi


def classify(vals, low_cut, step):
    """Decide what a Field's low readings are, from its value history alone.

    Returns a dict with the facts the verdict is built on and a `kind`:
      "never-dark" — the field never reads below `low_cut`; it can never make
                     the group all-DARK, so it only ever blocks detection;
      "zero"       — the meter reports 0.0 and its low tail is exactly 0.0: no
                     offset, a DARK reading here is genuine zero current;
      "load"       — the meter reports 0.0 elsewhere, yet the low tail holds
                     non-zero values: those are real current, not an offset;
      "floor"      — the meter never reports 0.0 and the low tail is a tight
                     cluster (width ≤ 2 storage steps) at a non-zero value:
                     that value is the zero-current offset;
      "mixed"      — never reports 0.0, but the low tail is spread out: part
                     offset, part small load, not separable from history alone.
    """
    low = [v for v in vals if v < low_cut]
    zeros = sum(1 for v in vals if v <= 0.0)
    out = {"n": len(vals), "zeros": zeros, "low_n": len(low),
           "low_min": min(low) if low else None,
           "low_max": max(low) if low else None,
           "nonzero_low": [v for v in low if v > 0.0]}
    if not low:
        out["kind"] = "never-dark"
    elif zeros:
        out["kind"] = "load" if out["nonzero_low"] else "zero"
    elif (out["low_max"] - out["low_min"]) <= 2 * step:
        out["kind"] = "floor"
    else:
        out["kind"] = "mixed"
    return out


# ---------------------------------------------------------------- analysis

def analyse(con, cfg, name, low_cut, since_ts, wins, now, label=""):
    head = f"--- {name}{label} ---"
    print(f"\n  {head}")
    rows = read_history(con, name, since_ts)
    if len(rows) < 2:
        print("      <2 stored readings; nothing to conclude")
        return
    vals = [r["value"] for r in rows]
    ts = [r["ts"] for r in rows]
    dec = cfg.decimals_of(name)
    step = 10 ** -dec
    print(f"      readings={len(rows)}  span={fmt_ts(ts[0])} -> {fmt_ts(ts[-1])}"
          f"  ({fmt_dur(ts[-1] - ts[0])})")
    print(f"      value: min={min(vals):.3f}  max={max(vals):.3f}  "
          f"mean={statistics.mean(vals):.3f}  median={statistics.median(vals):.3f} A")
    print(f"      stored at {dec} decimals -> anything below {step / 2:.3f} A "
          f"reads as 0.0")

    # 1. can this meter express zero?
    zeros = sum(1 for v in vals if v <= 0.0)
    lowest = sorted({round(v, 3) for v in vals})[:6]
    print(f"      distinct lowest values: "
          f"{', '.join(f'{v:.3f}' for v in lowest)}")
    print(f"      readings at 0.0: {zeros} "
          f"({100.0 * zeros / len(vals):.2f}% of history)")

    # 2. shape of the low tail
    low = [v for v in vals if v < low_cut]
    if low:
        spread = statistics.pstdev(low) if len(low) > 1 else 0.0
        print(f"      low tail (< {low_cut} A): n={len(low)}"
              f"  min={min(low):.3f}  max={max(low):.3f}  mean={statistics.mean(low):.3f}"
              f"  sd={spread:.3f}  ({100.0 * len(low) / len(vals):.2f}% of history)")
    else:
        print(f"      low tail (< {low_cut} A): none — this field never reads DARK")
    g = biggest_gap(vals, low_cut * 4)
    if g:
        print(f"      largest empty interval under {low_cut * 4:.2f} A: "
              f"{g[0]:.3f} A wide, between {g[1]:.3f} and {g[2]:.3f}")

    # 3. does the low value also occur with power provably present?
    if wins:
        inside = [v for v, t in zip(vals, ts) if in_any(t, wins, now)]
        outside_low = [v for v, t in zip(vals, ts)
                       if not in_any(t, wins, now) and v < low_cut]
        print(f"      readings inside recorded blackout windows: {len(inside)}"
              + (f"  (min={min(inside):.3f} max={max(inside):.3f})" if inside else ""))
        print(f"      readings < {low_cut} A OUTSIDE every blackout window: "
              f"{len(outside_low)}"
              + (f"  (mean={statistics.mean(outside_low):.3f})" if outside_low else ""))
    else:
        outside_low = [v for v in vals if v < low_cut]
        print("      no recorded blackout windows for this group in the history")

    # ------------------------------------------------------------ verdict
    c = classify(vals, low_cut, step)
    print("      VERDICT:")
    if c["kind"] == "never-dark":
        print(f"        field never went below {low_cut} A — it cannot make the "
              f"group all-DARK; it only ever blocks detection.")
        return
    if c["kind"] == "zero":
        print(f"        meter DOES report 0.0 ({c['zeros']} times) and its whole "
              f"low tail is exactly 0.0 -> no offset on this field; a DARK "
              f"reading here is genuine zero current.")
    elif c["kind"] == "load":
        nz = c["nonzero_low"]
        print(f"        meter DOES report 0.0 ({c['zeros']} times) -> it can "
              f"express zero current, so the {len(nz)} non-zero low readings "
              f"([{min(nz):.3f}, {max(nz):.3f}] A) are REAL current, not an "
              f"offset.")
    elif c["kind"] == "floor":
        print(f"        meter NEVER reports 0.0; its lowest reading ever is "
              f"{c['low_min']:.3f} A and the whole low tail sits in "
              f"[{c['low_min']:.3f}, {c['low_max']:.3f}] -> FLOOR "
              f"(zero-current offset).")
    else:
        print(f"        meter NEVER reports 0.0, and the low tail spreads over "
              f"[{c['low_min']:.3f}, {c['low_max']:.3f}] -> mixed: part offset, "
              f"part small load; history alone cannot separate them.")
    if wins and outside_low:
        share = 100.0 * len(outside_low) / max(1, len(vals))
        print(f"        the same low value occurs with power provably present "
              f"({len(outside_low)} readings, {share:.1f}% of history) -> that "
              f"value is NOT evidence of an outage on this field alone.")
    if not wins:
        print("        (no blackout history to cross-check against)")


def group_report(con, cfg, group, args, now):
    print(f"\n{'=' * 70}\nGROUP {group.id!r}  ({group.info})")
    print(f"  below={group.below} A   for_seconds={group.for_seconds}s   "
          f"stale_after={group.stale_after}s")
    print(f"  watched fields: {', '.join(group.fields)}")
    wins = outage_windows(con, group.id)
    print(f"  recorded blackout windows: {len(wins)}")
    for a, b in wins[-5:]:
        print(f"    {fmt_ts(a)} -> "
              + (f"{fmt_ts(b)}  ({fmt_dur(b - a)})" if b else "(still open)"))
    low_cut = args.low if args.low is not None else group.below
    since_ts = now - args.days * 86400 if args.days else 0

    for name in group.fields:
        if name in cfg.sensors:
            analyse(con, cfg, name, low_cut, since_ts, wins, now)
            continue
        proxies = [p for p in stored_proxies(cfg, name) if p != name]
        print(f"\n  --- {name} ---")
        print("      Signal: never stored, no history in the DB.")
        if not proxies:
            print("      no stored current Field on the same Device to stand in "
                  "for it; use /listSignal for its live value.")
            continue
        print(f"      standing in with the same Device's stored current Fields: "
              f"{', '.join(proxies)}")
        for p in proxies:
            analyse(con, cfg, p, low_cut, since_ts, wins, now,
                    label=f"  (proxy for {name})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="history span (0 = all)")
    ap.add_argument("--field", action="append", default=[],
                    help="extra stored Field to analyse (name or glob); repeatable")
    ap.add_argument("--group", help="restrict to one blackout group id")
    ap.add_argument("--low", type=float,
                    help="low-tail cut-off in A (default: the group's `below`)")
    args = ap.parse_args()

    cfg = load_cfg()
    now = int(time.time())
    print(f"DB: {botdb.DB_PATH} (read-only)   now={fmt_ts(now)}")
    print(f"history span: {'all' if not args.days else str(args.days) + ' days'}")

    con = ro_conn()
    try:
        groups = cfg.blackouts
        if args.group:
            match = {k: v for k, v in groups.items()
                     if k.lower() == args.group.lower()}
            if not match:
                print(f"No blackout group {args.group!r}. "
                      f"Known: {', '.join(groups) or '(none)'}")
                return 2
            groups = match
        if not groups:
            print("No blackout groups configured (blackouts: block absent).")
        for group in groups.values():
            group_report(con, cfg, group, args, now)

        extra = []
        for pat in args.field:
            hits = [n for n in cfg.sensors if fnmatch.fnmatchcase(n, pat)]
            if not hits and cfg.resolve_sensor(pat) in cfg.sensors:
                hits = [cfg.resolve_sensor(pat)]
            if not hits:
                print(f"\n(no stored Field matches {pat!r})")
            extra.extend(h for h in hits if h not in extra)
        if extra:
            print(f"\n{'=' * 70}\nEXTRA FIELDS")
            low_cut = args.low if args.low is not None else 0.5
            since_ts = now - args.days * 86400 if args.days else 0
            for name in extra:
                analyse(con, cfg, name, low_cut, since_ts, [], now)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
