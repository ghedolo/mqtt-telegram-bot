#!/usr/bin/env python3
"""Measure each sensor's real publish cadence, and suggest an `interval` for it
(READ-ONLY — the DB is opened read-only and never written).

`interval` is the one setting behind offline detection: a Device is declared
OFFLINE after `3 x interval` of silence. Set it too low and every normal gap
becomes a false alarm; too high (the 300 s default on a device that publishes
every 4 s) and a dead sensor goes unnoticed for a quarter of an hour. This
prints what the data actually says, per sensor.

Run inside the deploy (cwd = /app, so data/sensors.db is present):

  docker compose exec -T bot python3 - < cadence.py
  docker compose exec -T bot python3 - 'SM1_*' --days 7 < cadence.py

Feeding the script on stdin avoids a bind-mount and avoids pasting an indented
one-liner into a shell, which Python reads as an unexpected indented block.

Columns:
  samples   readings used (the `readings` table only — anything already moved
            to `readings_archive` by retention is out of scope)
  median    typical gap between readings
  p90       gap that 90% of readings beat: the sane basis for `interval`
  max       worst observed gap
  last      age of the newest reading
  now       `interval` currently configured, when the config can be read
  suggest   p90 rounded up, and the OFFLINE delay (3x) it implies

A `max` above 3 x suggest is flagged: an outage that long already happened, so
that `interval` would have raised an OFFLINE for it. Decide whether that gap
was a real outage (fine, alarm wanted) or normal jitter (raise the interval).
"""
import argparse
import fnmatch
import math
import sqlite3
import statistics
import sys
import time

DEFAULT_DB = "data/sensors.db"


def _ro_conn(path: str):
    """Open the readings DB strictly read-only — cannot create or mutate it."""
    uri = f"file:{path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def _pct(sorted_vals: list[int], q: float) -> int:
    i = min(len(sorted_vals) - 1, int(q * (len(sorted_vals) - 1) + 0.5))
    return sorted_vals[i]


def _fmt_secs(s: float | None) -> str:
    if s is None:
        return "-"
    s = int(s)
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _round_up(seconds: float) -> int:
    """Round a measured gap up to a tidy interval — nobody wants `interval: 63`.

    The step stays proportional: rounding a 62 s meter to a whole minute would
    give 120, doubling its OFFLINE delay for the sake of a round number."""
    s = math.ceil(seconds)
    if s <= 60:
        return s
    if s <= 600:
        return math.ceil(s / 10) * 10
    return math.ceil(s / 60) * 60


def configured_intervals() -> dict[str, int]:
    """Each sensor's configured `interval`, or {} when the config is not
    loadable from here (running outside the deploy, no credentials.yaml).
    Purely informational: the measurement itself needs no config."""
    try:
        from bot.config import load
        cfg = load("sensors.d", "credentials.yaml")
    except Exception as e:
        print(f"(config not readable, 'now' column omitted: {e})\n", file=sys.stderr)
        return {}
    return {name: sc.interval for name, sc in cfg.sensors.items()}


def sensor_names(con, patterns: list[str]) -> list[str]:
    all_names = [r["sensor"] for r in con.execute(
        "SELECT DISTINCT sensor FROM readings ORDER BY sensor"
    )]
    if not patterns:
        return all_names
    picked = [n for n in all_names if any(fnmatch.fnmatch(n, p) for p in patterns)]
    return picked


def measure(con, name: str, since_ts: int, limit: int) -> dict:
    rows = con.execute(
        "SELECT ts FROM readings WHERE sensor=? AND ts>=? ORDER BY ts DESC LIMIT ?",
        (name, since_ts, limit),
    ).fetchall()
    ts = [r["ts"] for r in rows]                     # newest first
    stat = {"name": name, "samples": len(ts), "last": None,
            "median": None, "p90": None, "max": None}
    if ts:
        stat["last"] = int(time.time()) - ts[0]
    if len(ts) < 2:
        return stat
    gaps = sorted(a - b for a, b in zip(ts, ts[1:]))
    stat["median"] = statistics.median(gaps)
    stat["p90"] = _pct(gaps, 0.90)
    stat["max"] = gaps[-1]
    return stat


def report(stats: list[dict], configured: dict[str, int]):
    head = ("Sensor", "samples", "median", "p90", "max", "last", "now", "suggest", "offline after")
    rows = []
    flags = []
    for s in stats:
        if s["p90"] is None:
            rows.append((s["name"], str(s["samples"]), "-", "-", "-",
                         _fmt_secs(s["last"]), str(configured.get(s["name"], "")) or "-",
                         "-", "too few readings"))
            continue
        suggest = _round_up(s["p90"])
        offline_after = suggest * 3
        rows.append((
            s["name"], str(s["samples"]), _fmt_secs(s["median"]), _fmt_secs(s["p90"]),
            _fmt_secs(s["max"]), _fmt_secs(s["last"]),
            str(configured.get(s["name"], "")) or "-",
            str(suggest), _fmt_secs(offline_after),
        ))
        if s["max"] > offline_after:
            flags.append(
                f"  {s['name']}: worst gap {_fmt_secs(s['max'])} exceeds 3 x {suggest}s "
                f"({_fmt_secs(offline_after)}) — with interval {suggest} that gap would "
                f"have raised an OFFLINE. Real outage, or raise the interval."
            )

    w = [max(len(head[i]), *(len(r[i]) for r in rows)) for i in range(len(head))] if rows else []
    if not rows:
        print("No sensors matched.")
        return
    print("  ".join(h.ljust(w[i]) for i, h in enumerate(head)).rstrip())
    print("  ".join("-" * w[i] for i in range(len(head))))
    for r in rows:
        print("  ".join(r[i].ljust(w[i]) for i in range(len(head))).rstrip())
    print("\n'suggest' is p90 rounded up; OFFLINE fires after 3 x interval.")
    if flags:
        print("\nWorth a look:")
        for f in flags:
            print(f)


def main():
    ap = argparse.ArgumentParser(description="Measure sensor publish cadence (read-only)")
    ap.add_argument("patterns", nargs="*",
                    help="sensor names or globs (default: all). Quote them: 'SM1_*'")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--days", type=int, default=7,
                    help="history span in days (0 = all available; default 7)")
    ap.add_argument("--limit", type=int, default=2000,
                    help="max readings per sensor (default 2000)")
    ap.add_argument("--no-config", action="store_true",
                    help="skip reading sensors.d/ for the configured interval")
    args = ap.parse_args()

    since_ts = int(time.time()) - args.days * 86400 if args.days else 0
    con = _ro_conn(args.db)
    try:
        names = sensor_names(con, args.patterns)
        if not names:
            sys.exit("No sensor in the DB matched the given pattern(s)")
        stats = [measure(con, n, since_ts, args.limit) for n in names]
    finally:
        con.close()
    configured = {} if args.no_config else configured_intervals()
    report(stats, configured)


if __name__ == "__main__":
    main()
