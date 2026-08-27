#!/usr/bin/env python3
"""Why did no message arrive? — read-only diagnosis of a silent or frozen Field.

Answers, per watched Field, the two questions the blackout state machine cannot
answer by itself:

  1. is the meter MUTE (stopped publishing) or FROZEN (still publishing, always
     the same value)? — the first is what the per-Device OFFLINE alarm exists to
     report, the second is detected by nothing in the bot today;
  2. if it is mute, WHY did no OFFLINE DM arrive? — the filters between a silent
     Field and a Telegram message are each checked and printed:
       - a Signal (`signal: true`) is excluded from the offline check entirely;
       - offline is per-Device and reads the newest message across ALL its
         topics, so one live Field masks a dead one on the same Device;
       - the DM needs an admin of that Field who is DM-registered AND has a
         digest subscription on the Field name (a subscription to the blackout
         group id does not deliver offline);
       - an `ackOff` silence suppresses it.

READ-ONLY: the DB is opened with mode=ro and never written, the config is only
read. Nothing here changes bot state.

Run inside the deploy (cwd = /app, so data/sensors.db and sensors.d/ are there):

  docker compose exec -T bot python3 - < tools/silence_diag.py
  docker compose exec -T bot python3 - --days 90 'SM1_*' < tools/silence_diag.py

Feeding the script on stdin avoids a bind-mount. Default scope is every Field
named by a blackout group (stored Sensors and Signals alike); extra sensor names
or globs may be added as positional arguments.

A Signal's value lives only in the bot's memory, never in the DB, so this script
can report its config-level exposure but not its value — use `/listSignal` for
that.
"""
import argparse
import fnmatch
import sqlite3
import sys
import time

DEFAULT_DB = "data/sensors.db"
NOW = int(time.time())


# ---------------------------------------------------------------- helpers

def ro_conn(path: str):
    """Open the readings DB strictly read-only — cannot create or mutate it."""
    uri = f"file:{path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def has_table(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def ago(ts: int | None) -> str:
    return dur(NOW - ts) if ts else "-"


def dur(s: float | None) -> str:
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


def table(head: tuple, rows: list[tuple]):
    if not rows:
        print("  (none)")
        return
    cols = len(head)
    w = [max(len(str(head[i])), *(len(str(r[i])) for r in rows)) for i in range(cols)]
    print("  " + "  ".join(str(head[i]).ljust(w[i]) for i in range(cols)).rstrip())
    print("  " + "  ".join("-" * w[i] for i in range(cols)))
    for r in rows:
        print("  " + "  ".join(str(r[i]).ljust(w[i]) for i in range(cols)).rstrip())


def section(title: str):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def load_cfg():
    """The live config, or None when it is not readable from here (running
    outside the deploy). Without it the DB-only sections still work."""
    try:
        from bot.config import load
        return load("sensors.d", "credentials.yaml")
    except Exception as e:
        print(f"!! config not readable ({e}) — running DB-only, sections B/C/D "
              f"will be partial", file=sys.stderr)
        return None


# ------------------------------------------------------- measure & decide

def read_history(con, sensor: str, since_ts: int, limit: int) -> list[sqlite3.Row]:
    """Newest-first (ts, value) for a sensor, across `readings` and
    `readings_archive` — a freeze can easily be older than retention."""
    q = "SELECT ts, value FROM readings WHERE sensor=? AND ts>=?"
    args: list = [sensor, since_ts]
    if has_table(con, "readings_archive"):
        q += " UNION ALL SELECT ts, value FROM readings_archive WHERE sensor=? AND ts>=?"
        args += [sensor, since_ts]
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return con.execute(q, args).fetchall()


def analyse(rows) -> dict:
    """Mute-vs-frozen facts from a newest-first history.

    `frozen_for` is the length of the flat run ending at the newest reading. If
    the value never changed inside the window the run is longer than measured,
    and `frozen_capped` says so — the number is a lower bound, not the truth."""
    out = {"samples": len(rows), "last_ts": None, "last_value": None,
           "changed_ts": None, "frozen_for": None, "frozen_capped": False,
           "max_gap": None, "typ_gap": None, "distinct": 0}
    if not rows:
        return out
    out["last_ts"] = rows[0]["ts"]
    out["last_value"] = rows[0]["value"]
    out["distinct"] = len({r["value"] for r in rows})
    for r in rows:
        if r["value"] != out["last_value"]:
            out["changed_ts"] = r["ts"]
            break
    if out["changed_ts"] is not None:
        out["frozen_for"] = out["last_ts"] - out["changed_ts"]
    else:
        out["frozen_for"] = out["last_ts"] - rows[-1]["ts"]
        out["frozen_capped"] = True
    ts = [r["ts"] for r in rows]
    if len(ts) > 1:
        gaps = sorted(a - b for a, b in zip(ts, ts[1:]))
        out["max_gap"] = gaps[-1]
        out["typ_gap"] = gaps[len(gaps) // 2]     # median gap = real cadence
    return out


def verdict(a: dict, offline_after: int | None, now: int = None) -> str:
    """MUTE / FROZEN / OK for one field.

    MUTE uses the same rule as `check_offline` — silence beyond `3 x interval`.
    Without the config that threshold is unknown, so the measured cadence stands
    in for `interval`: a field publishing every 60 s that has said nothing for
    two days is mute whether or not sensors.d/ can be read from here."""
    now = NOW if now is None else now
    if a["last_ts"] is None:
        return "NO DATA in window"
    measured_after = a["typ_gap"] * 3 if a["typ_gap"] else None
    limit = offline_after or measured_after
    if limit and (now - a["last_ts"]) > limit:
        src = "3x interval" if offline_after else "3x measured cadence"
        return f"MUTE (silent > {src} = {dur(limit)})"
    if a["distinct"] == 1 and a["samples"] > 2:
        return "FROZEN (one single value in the whole window)"
    if a["frozen_for"] and limit and a["frozen_for"] > 10 * limit:
        return f"FROZEN for {dur(a['frozen_for'])}"
    return "OK (publishing and changing)"


def classify(value: float, age: int, group, is_valid: bool) -> str:
    """The per-field state the blackout machine would read right now, by the
    same order of tests as `AlarmManager._classify_fields`."""
    if age > group.stale_after:
        return "STALE"
    if not is_valid:
        return "INVALID"
    return "LIT" if value >= group.below else "DARK"


def group_verdict(states: list[str]) -> str:
    """What the group would do, given its fields' states. UNKNOWN covers
    STALE/INVALID/MISSING — none of them is evidence either way."""
    seen = ["DARK" if s == "DARK" else "LIT" if s == "LIT" else "UNKNOWN"
            for s in states]
    if seen and all(s == "DARK" for s in seen):
        return "ALL DARK — blackout would be raised or held"
    if "LIT" in seen:
        return "at least one LIT — POWERED (an active blackout would end)"
    return "only UNKNOWN, no LIT — HOLD, the machine sends NOTHING"


def dm_targets(candidates: set[int], registered: set[int],
               subs: dict[int, set[str]], key: str) -> set[int]:
    """Users who would actually receive a DM about `key`: access holders who are
    DM-registered and subscribed to that exact key. Mirrors the AND in
    `TelegramBot.notify_device` / `notify_blackout` — the step that most often
    turns a fired alarm into a message nobody sees."""
    return {u for u in candidates if u in registered and key in subs.get(u, set())}


# ---------------------------------------------------------------- sections

def scope(cfg, extra_patterns, con) -> list[tuple[str, str, str]]:
    """(field, kind, group id) for everything to examine.

    Beyond the Fields a blackout group names, this pulls in the **stored
    siblings** of each one's Device: a group that watches only Signals would
    otherwise produce an empty report, since a Signal has no history to read —
    while the Device's stored current is exactly what says whether the meter
    went quiet. The siblings are tagged `<group id>*`."""
    watched: list[tuple[str, str, str]] = []
    if cfg is not None:
        for gid, grp in cfg.blackouts.items():
            for name in grp.fields:
                kind = ("signal" if name in cfg.signals
                        else "sensor" if name in cfg.sensors else "UNKNOWN")
                watched.append((name, kind, gid))
        known = {n for n, _, _ in watched}
        for gid, grp in cfg.blackouts.items():
            for name in grp.fields:
                dev = cfg.devices.get(cfg.device_of(name))
                if dev is None:
                    continue
                for osc in dev.fields.values():
                    if osc.name not in known:
                        known.add(osc.name)
                        watched.append((osc.name, "sensor", f"{gid}*"))
    if extra_patterns:
        known = {n for n, _, _ in watched}
        for r in con.execute("SELECT DISTINCT sensor FROM readings ORDER BY sensor"):
            n = r["sensor"]
            if n not in known and any(fnmatch.fnmatch(n, p) for p in extra_patterns):
                watched.append((n, "sensor", ""))
    return watched


def sec_fields(cfg, con, watched, since_ts, limit) -> dict:
    section("A. Per-Field: mute or frozen?")
    print("last      = age of the newest stored reading (a Signal is never stored)")
    print("frozen    = how long the value has been identical ('+' = window-capped)")
    print("max gap   = worst silence inside the window")
    print("verdict   = MUTE (no fresh reading) / FROZEN (fresh but unchanging) / OK")
    print()
    rows = []
    facts = {}
    for name, kind, gid in watched:
        if kind == "signal":
            rows.append((name, kind, gid or "-", "-", "-", "-", "-", "-",
                         "NOT STORED — see section C"))
            facts[name] = None
            continue
        a = analyse(read_history(con, name, since_ts, limit))
        facts[name] = a
        sc = cfg.sensors.get(name) if cfg else None
        offline_after = sc.interval * 3 if sc else None
        rows.append((
            name, kind, gid or "-", a["samples"], ago(a["last_ts"]),
            "-" if a["last_value"] is None else f"{a['last_value']:g}",
            dur(a["frozen_for"]) + ("+" if a["frozen_capped"] else ""),
            dur(a["max_gap"]), verdict(a, offline_after),
        ))
    table(("field", "kind", "group", "samples", "last", "value",
           "frozen", "max gap", "verdict"), rows)
    print()
    print("A FROZEN field is detected by NOTHING in the bot today: its reading is")
    print("fresh, so it is classified LIT or DARK on a value nobody is updating.")
    return facts


def sec_groups(cfg, facts):
    section("B. Blackout groups: what the machine sees right now")
    if cfg is None:
        print("  (config unreadable)")
        return
    if not cfg.blackouts:
        print("  (no blackout group configured)")
        return
    for gid, grp in cfg.blackouts.items():
        print(f"\n[{gid}] below={grp.below} for_seconds={grp.for_seconds} "
              f"stale_after={grp.stale_after} repeat_seconds={grp.repeat_seconds}")
        rows, states = [], []
        for name in grp.fields:
            if name in cfg.signals:
                rows.append((name, "signal", "-", "-",
                             "in-memory only — check /listSignal"))
                states.append("UNKNOWN")
                continue
            a = facts.get(name)
            if not a or a["last_ts"] is None:
                rows.append((name, "sensor", "-", "-", "MISSING"))
                states.append("MISSING")
                continue
            age = NOW - a["last_ts"]
            v = a["last_value"]
            st = classify(v, age, grp, cfg.is_valid(name, v))
            states.append(st)
            rows.append((name, "sensor", f"{v:g}", dur(age), st))
        table(("field", "kind", "value", "age", "state"), rows)
        print(f"  -> group: {group_verdict(states)}")


def sec_offline_coverage(cfg, con, watched):
    section("C. Would an OFFLINE alarm ever fire for these Fields?")
    if cfg is None:
        print("  (config unreadable)")
        return
    rows = []
    for name, kind, _gid in watched:
        if kind == "signal":
            rows.append((name, "-", "-", "-",
                         "NEVER — a Signal is excluded from the per-Device offline check"))
            continue
        sc = cfg.sensors.get(name)
        if sc is None:
            rows.append((name, "?", "-", "-", "field not in config"))
            continue
        dev = cfg.devices.get(sc.device_key)
        if dev is None:
            note = "device not in config"
        elif dev.availability_topic:
            note = "z2m availability drives offline (data cadence ignored)"
        else:
            mine = con.execute("SELECT MAX(ts) AS t FROM readings WHERE sensor=?",
                               (name,)).fetchone()["t"]
            fresher = []
            for osc in dev.fields.values():
                if osc.name == name:
                    continue
                t = con.execute("SELECT MAX(ts) AS t FROM readings WHERE sensor=?",
                                (osc.name,)).fetchone()["t"]
                if t and (mine is None or t > mine):
                    fresher.append((osc.name, t))
            note = ("MASKED — same device, fresher field(s): "
                    + ", ".join(f"{n} {ago(t)} ago" for n, t in fresher)
                    ) if fresher else "device silence == this field's silence"
        rows.append((name, sc.device_key, sc.interval, dur(sc.interval * 3), note))
    table(("field", "device", "interval", "offline after", "note"), rows)
    print()
    print("OFFLINE is per-DEVICE and reads the newest message across all its topics:")
    print("one live field keeps a device 'online' while another one is dead.")


def sec_recipients(cfg, con, watched):
    section("D. Who would actually receive the DM?")
    if cfg is None:
        print("  (config unreadable)")
        return
    registered = {r[0] for r in con.execute("SELECT chat_id FROM dm_registered")}
    subs: dict[int, set[str]] = {}
    for uid, sensor in con.execute("SELECT user_id, sensor FROM digest_subscriptions"):
        subs.setdefault(uid, set()).add(sensor)

    print("\nOFFLINE (needs: admin of the field  AND  DM-registered  AND  digest ON the field name)")
    rows = []
    for name, kind, _gid in watched:
        if kind == "signal":
            rows.append((name, "-", "-", "-", "no offline alarm exists for a Signal"))
            continue
        admins = cfg.admins_of(name)
        final = dm_targets(admins, registered, subs, name)
        rows.append((name, len(admins), len(admins & registered), len(final),
                     ",".join(str(u) for u in sorted(final)) or "NOBODY — no DM sent"))
    table(("field", "admins", "registered", "would get DM", "recipients"), rows)

    print("\nBLACKOUT (needs: viewer of a watched field  AND  DM-registered  AND  digest ON the group id)")
    rows = []
    for gid in cfg.blackouts:
        viewers = cfg.viewers_of_blackout(gid)
        final = dm_targets(viewers, registered, subs, gid)
        rows.append((gid, len(viewers), len(viewers & registered), len(final),
                     ",".join(str(u) for u in sorted(final)) or "NOBODY — no DM sent"))
    table(("group", "viewers", "registered", "would get DM", "recipients"), rows)

    print("\nNOTE: a /digest subscription to the GROUP id does not deliver OFFLINE —")
    print("offline delivery is keyed on the FIELD name, and needs admin rights.")

    print("\nDM registration age — a recipient registered after the event was not one")
    print("at the time (the tables hold today's state, not the state back then):")
    table(("chat_id", "registered"),
          [(r["chat_id"], ago(r["registered_at"])) for r in con.execute(
              "SELECT chat_id, registered_at FROM dm_registered ORDER BY registered_at DESC")])

    print("\nDigest subscriptions per user (the key an alarm is delivered on):")
    rows = []
    for uid in sorted(subs):
        rows.append((uid, ", ".join(sorted(subs[uid]))[:90]))
    table(("user", "subscribed to"), rows)

    print("\nActive mutes (they suppress THRESHOLD DMs only, never OFFLINE):")
    if has_table(con, "mutes"):
        table(("chat_id", "sensor", "until"),
              [(r["chat_id"], r["sensor"],
                time.strftime("%Y-%m-%d %H:%M", time.localtime(r["until_ts"])))
               for r in con.execute(
                   "SELECT chat_id, sensor, until_ts FROM mutes WHERE until_ts>=? "
                   "ORDER BY chat_id", (NOW,))])
    else:
        print("  (no mutes table)")

    print("\nackOff silences in force (they suppress OFFLINE DMs):")
    if has_table(con, "silenced"):
        table(("subject", "since"),
              [(r["sensor"], ago(r["silenced_at"])) for r in con.execute(
                  "SELECT sensor, silenced_at FROM silenced ORDER BY sensor")])
    else:
        print("  (no silenced table)")


def sec_alarms(cfg, con, watched, since_ts):
    section("E. Alarm history for the subjects involved")
    subjects = {n for n, _, _ in watched}
    if cfg is not None:
        subjects |= set(cfg.blackouts)
        subjects |= {cfg.device_of(n) for n, _, _ in watched if cfg.device_of(n)}
    else:
        # no config: guess the device key from `{device}_{field}` so the
        # device's OFFLINE rows still show up
        subjects |= {n.rsplit("_", 1)[0] for n, _, _ in watched if "_" in n}
    qmarks = ",".join("?" * len(subjects))
    rows = con.execute(
        f"SELECT ts, sensor, kind, message FROM alarms "
        f"WHERE sensor IN ({qmarks}) AND ts>=? ORDER BY ts DESC LIMIT 60",
        (*sorted(subjects), since_ts),
    ).fetchall()
    table(("when", "subject", "kind", "message"),
          [(time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"])),
            r["sensor"], r["kind"], r["message"][:90]) for r in rows])
    print()
    print("No OFFLINE row for a device whose field is mute means the alarm never")
    print("fired (section C says why); a row whose recipients are 0 in section D")
    print("means it fired and nobody was told.")


def sec_all_alarms(con, limit: int):
    """The last N alarm rows, whatever the subject.

    Section E is scoped to the fields under examination, which hides the one
    thing that separates a per-meter fault from a bot-wide one: whether every
    other device went offline at the same moment (a dropped MQTT session, a
    broker outage) or only these did."""
    section(f"G. Last {limit} alarm rows — any subject")
    rows = con.execute(
        "SELECT ts, sensor, kind, message FROM alarms ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    table(("when", "subject", "kind", "message"),
          [(time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"])),
            r["sensor"], r["kind"], r["message"][:80]) for r in rows])
    print()
    print("Several devices going OFFLINE within the same minute points at the bot's")
    print("own MQTT session, not at the meters; one device alone points at the meter.")


def sec_health(cfg, con, watched):
    """Is the bot still ingesting and still checking, or did it stop?

    A device that is mute while the whole DB is mute is not a sensor problem:
    the repeats stop because nothing is running, not because anything recovered.
    The distinction is invisible from the alarm history alone — a stopped bot
    and a recovered device both simply stop producing rows."""
    section("F. Process health — is anything still being written?")
    newest = con.execute("SELECT MAX(ts) AS t FROM readings").fetchone()["t"]
    last_alarm = con.execute("SELECT MAX(ts) AS t FROM alarms").fetchone()["t"]
    fresh_1h = con.execute("SELECT COUNT(*) AS c FROM readings WHERE ts>=?",
                           (NOW - 3600,)).fetchone()["c"]
    live = con.execute("SELECT COUNT(*) AS c FROM (SELECT sensor FROM readings "
                       "WHERE ts>=? GROUP BY sensor)", (NOW - 3600,)).fetchone()["c"]
    table(("what", "when", "age"), [
        ("newest reading in the DB (any sensor)",
         time.strftime("%Y-%m-%d %H:%M", time.localtime(newest)) if newest else "-",
         ago(newest)),
        ("newest alarm row (any subject)",
         time.strftime("%Y-%m-%d %H:%M", time.localtime(last_alarm)) if last_alarm else "-",
         ago(last_alarm)),
        ("readings stored in the last hour", str(fresh_1h), f"{live} sensor(s)"),
    ])
    if fresh_1h == 0:
        print("\n  !! NOTHING was stored in the last hour: the bot is not ingesting.")
        print("     Then no OFFLINE repeat and no ONLINE can be produced either —")
        print("     the alarm history going quiet says nothing about the meters.")

    print("\nLast stored reading per Device involved:")
    rows = []
    if cfg is not None:
        devs = {cfg.device_of(n) for n, _, _ in watched if cfg.device_of(n)}
        for dk in sorted(devs):
            dev = cfg.devices.get(dk)
            if dev is None:
                continue
            per = []
            for osc in dev.fields.values():
                t = con.execute("SELECT MAX(ts) AS t FROM readings WHERE sensor=?",
                                (osc.name,)).fetchone()["t"]
                per.append((osc.name, t))
            newest_dev = max((t for _, t in per if t), default=None)
            rows.append((dk, dev.interval, dur(dev.interval * 3), ago(newest_dev),
                         ", ".join(f"{n} {ago(t)}" for n, t in per) or "no stored field"))
        table(("device", "interval", "offline after", "last seen", "per field"), rows)
    else:
        print("  (config unreadable)")
    print()
    print("Since 1.6.6 the alarm state is rebuilt at startup from the newest row per")
    print("subject in `alarms`, so a Device that recovers while the bot is down still")
    print("gets its 'back online'. Before that a restart lost the state silently — an")
    print("OFFLINE with no matching ONLINE around a restart is that old behaviour.")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="Diagnose a mute/frozen Field and the message that never came (read-only)")
    ap.add_argument("patterns", nargs="*",
                    help="extra sensor names or globs beyond the blackout fields. Quote them: 'SM1_*'")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--days", type=int, default=30,
                    help="history window in days (0 = all available; default 30)")
    ap.add_argument("--limit", type=int, default=20000,
                    help="max readings scanned per field (default 20000)")
    ap.add_argument("--tail-alarms", type=int, default=30,
                    help="how many recent alarm rows to list regardless of subject (default 30)")
    args = ap.parse_args()

    since_ts = NOW - args.days * 86400 if args.days else 0
    cfg = load_cfg()
    con = ro_conn(args.db)
    try:
        watched = scope(cfg, args.patterns, con)
        print(f"DB={args.db}  window={args.days or 'all'}d  "
              f"now={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(NOW))}")
        if not watched:
            sys.exit("No field to examine (no blackout group configured, no pattern matched)")
        facts = sec_fields(cfg, con, watched, since_ts, args.limit)
        sec_groups(cfg, facts)
        sec_offline_coverage(cfg, con, watched)
        sec_recipients(cfg, con, watched)
        sec_alarms(cfg, con, watched, since_ts)
        sec_health(cfg, con, watched)
        sec_all_alarms(con, args.tail_alarms)
    finally:
        con.close()


if __name__ == "__main__":
    main()
