#!/usr/bin/env python3
"""Blackout-detection diagnostics (READ-ONLY).

Answers, from real production data, three questions:
  1. What is the meters' actual publish cadence (the sampling floor)?
  2. Given for_seconds / stale_after, what is the shortest blackout this
     config could POSSIBLY detect?
  3. For a specific window (e.g. yesterday 19:00-20:00), replay the exact
     detection state machine over the recorded readings and show, reading by
     reading, why it did or did not fire.

Run inside the deploy (cwd = /app, so sensors.d/, credentials.yaml and
data/sensors.db are all present). It NEVER writes: the DB is opened read-only.

  docker compose exec -T bot python - \
      --around "2026-07-15 19:00" --window-min 90 < blackout_diag.py

Args:
  --around "YYYY-MM-DD HH:MM"  centre of the event window to trace (local time)
  --window-min N               half-width of that window, minutes (default 45)
  --days N                     history span for the cadence stats (0 = all)
"""
import argparse
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone

from bot import config
from bot import db as botdb


def _ro_conn():
    """Open the readings DB strictly read-only — cannot create or mutate it."""
    uri = f"file:{botdb.DB_PATH}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def _fmt_ts(ts):
    loc = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    utc = datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S")
    return f"{loc} (local) / {utc}Z"


def _pct(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, int(q * (len(sorted_vals) - 1) + 0.5))
    return sorted_vals[i]


def load_cfg():
    return config.load("sensors.d", "credentials.yaml")


def cadence_report(con, group, days=0):
    print(f"\n{'='*70}\nGROUP {group.id!r}  ({group.info})")
    print(f"  below={group.below} A   for_seconds={group.for_seconds}s   "
          f"stale_after={group.stale_after}s   repeat={group.repeat_seconds}s")
    print(f"  watched fields: {', '.join(group.fields)}")
    since_ts = int(time.time()) - days * 86400 if days else 0

    worst_median = 0
    for name in group.fields:
        rows = con.execute(
            "SELECT value, ts FROM readings WHERE sensor=? AND ts>=? ORDER BY ts ASC",
            (name, since_ts),
        ).fetchall()
        print(f"\n  --- {name} ---")
        if len(rows) < 2:
            print("      <2 readings; cannot measure cadence")
            continue
        ts = [r["ts"] for r in rows]
        vals = [r["value"] for r in rows]
        gaps = sorted(ts[i + 1] - ts[i] for i in range(len(ts) - 1))
        med = statistics.median(gaps)
        worst_median = max(worst_median, med)
        below_ct = sum(1 for v in vals if v < group.below)
        span_h = (ts[-1] - ts[0]) / 3600
        print(f"      readings={len(rows)}  span={span_h:.1f}h  "
              f"first={_fmt_ts(ts[0])}  last={_fmt_ts(ts[-1])}")
        print(f"      inter-reading gap: median={med}s  p95={_pct(gaps,0.95)}s  "
              f"max={gaps[-1]}s")
        print(f"      value: min={min(vals):.3f}  max={max(vals):.3f}  "
              f"mean={statistics.mean(vals):.3f} A")
        print(f"      readings below {group.below} A (DARK samples): {below_ct}")

    floor = max(group.for_seconds, 2 * worst_median)
    print(f"\n  >>> RESOLUTION FLOOR for {group.id!r}:")
    print(f"      sampling floor  ≈ {worst_median}s (slowest field's median cadence)")
    print(f"      sustain floor   = {group.for_seconds}s (for_seconds)")
    print(f"      A blackout must last ≳ {floor}s AND span ≥2 readings to be "
          f"detectable at all.")
    return worst_median


def replay(con, group, t0, t1):
    """Replay the EXACT detection algorithm (mirrors AlarmManager.check_blackout)
    over every watched-field reading in [t0, t1], printing the state each step."""
    print(f"\n{'='*70}\nREPLAY {group.id!r}  {_fmt_ts(t0)}  ->  {_fmt_ts(t1)}")
    # pull all readings for the group's fields in-window, plus a lead-in so
    # 'latest reading' is populated before the window starts
    lead = max(group.stale_after * 2, 300)
    events = []
    for name in group.fields:
        for r in con.execute(
            "SELECT value, ts FROM readings WHERE sensor=? AND ts>=? AND ts<=? "
            "ORDER BY ts ASC",
            (name, t0 - lead, t1),
        ).fetchall():
            events.append((r["ts"], name, r["value"]))
    events.sort()
    if not events:
        print("  (no readings for these fields in the window)")
        return

    latest = {}                      # field -> (value, ts)
    since = 0                        # SUSPECTED timer start (0 = POWERED)
    active = False                   # OUTAGE
    last_notified = 0
    raised = 0
    max_spell = 0                    # longest all-DARK spell reached (s)
    printed_header = False
    powered_run = 0                  # collapse consecutive quiet POWERED rows

    def flush_powered():
        nonlocal powered_run
        if powered_run:
            print(f"  {'   … ':<21}  {'':<10}  ({powered_run} readings POWERED, "
                  f"all fields LIT — no dip)")
            powered_run = 0

    for now, name, value in events:
        latest[name] = (value, now)
        all_dark, any_lit, states = True, False, {}
        for f in group.fields:
            lv = latest.get(f)
            if lv is None:
                states[f] = "UNK(none)"; all_dark = False; continue
            fresh = (now - lv[1]) <= group.stale_after
            if not fresh:
                states[f] = f"UNK({value_age(now, lv[1])})"; all_dark = False
            elif lv[0] >= group.below:
                states[f] = f"LIT({lv[0]:.2f})"; all_dark = False; any_lit = True
            else:
                states[f] = f"DARK({lv[0]:.2f})"

        group_state = "POWERED"
        event_note = ""
        if all_dark:
            if since == 0:
                since = now
            spell = now - since
            max_spell = max(max_spell, spell)
            if spell >= group.for_seconds and not active:
                active = True; last_notified = now; raised += 1
                group_state = "OUTAGE"; event_note = "  <== ⚡ WOULD RAISE"
            elif active:
                group_state = "OUTAGE"
                if now - last_notified >= group.repeat_seconds:
                    last_notified = now; event_note = "  <== ⚡ repeat"
            else:
                group_state = f"SUSPECTED(+{spell}s/{group.for_seconds}s)"
        elif any_lit:
            if active:
                event_note = "  <== 🔌 WOULD END"
            since = 0; active = False; group_state = "POWERED"
        else:
            group_state = "HOLD(stale)"   # some UNKNOWN, no LIT: freeze

        if t0 <= now <= t1:
            if not printed_header:
                print(f"  {'time':<21}  {'trigger':<10}  fields -> group")
                printed_header = True
            quiet = group_state == "POWERED" and not event_note
            if quiet:
                powered_run += 1
            else:
                flush_powered()
                fstr = "  ".join(f"{k}={v}" for k, v in states.items())
                print(f"  {_fmt_ts(now).split(' (')[0]:<21}  {name:<10}  "
                      f"{fstr}  => {group_state}{event_note}")
    flush_powered()

    print(f"\n  RESULT: blackouts the algorithm would raise in-window = {raised}")
    print(f"  longest all-DARK spell reached = {max_spell}s "
          f"(needs ≥ {group.for_seconds}s to raise)")


def value_age(now, ts):
    return f"{now - ts}s old"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--around", help='event centre, local "YYYY-MM-DD HH:MM"')
    ap.add_argument("--window-min", type=int, default=45)
    ap.add_argument("--days", type=int, default=0, help="cadence history span (0=all)")
    args = ap.parse_args()

    cfg = load_cfg()
    if not cfg.blackouts:
        print("No blackout groups configured (blackouts: block absent).")
        return
    print(f"Blackout groups: {', '.join(cfg.blackouts)}")
    print(f"DB: {botdb.DB_PATH} (read-only)   now={_fmt_ts(int(time.time()))}")

    con = _ro_conn()
    try:
        for group in cfg.blackouts.values():
            cadence_report(con, group, args.days)
            if args.around:
                try:
                    centre = datetime.strptime(args.around, "%Y-%m-%d %H:%M").timestamp()
                except ValueError:
                    print(f"\n  bad --around {args.around!r}; expected 'YYYY-MM-DD HH:MM'")
                    continue
                half = args.window_min * 60
                replay(con, group, int(centre - half), int(centre + half))
    finally:
        con.close()


if __name__ == "__main__":
    main()
