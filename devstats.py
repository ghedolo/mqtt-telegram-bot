#!/usr/bin/env python3
"""
Cumulative development-effort stats for the README.

Unlike a plain recompute, this keeps a committed ledger (devstats.json) keyed by
Claude Code session UUID, so effort ACCUMULATES across machines and survives
transcript pruning. Run at the end of a work session, on any machine:

    python3 devstats.py
    git add devstats.json README.md && git commit -m "chore: devstats" && git push

It scans THIS machine's transcripts, updates each local session's entry in the
ledger (idempotent — re-running just refreshes the same UUIDs; other machines'
entries are left untouched because they already live in the committed ledger),
then re-renders the README from the whole ledger: a frozen pre-ledger baseline
plus every recorded session. The transcript directory is derived from this
script's own location, so it is portable across machines.
"""
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
README = os.path.join(HERE, "README.md")
LEDGER = os.path.join(HERE, "devstats.json")


def transcript_dir():
    """Claude Code stores a project's transcripts under ~/.claude/projects/<p>,
    where <p> is the project path with separators replaced by '-'. Derive it
    from this script's directory so it resolves correctly on any machine."""
    dashed = HERE.replace(os.sep, "-").replace(".", "-")
    return os.path.expanduser(os.path.join("~/.claude/projects", dashed))


def session_stats(path):
    ts, c = [], dict(user=0, asst=0, input=0, output=0, cache_read=0, cache_write=0)
    with open(path) as f:
        for line in f:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("timestamp"):
                ts.append(obj["timestamp"])
            t = obj.get("type", "")
            if t == "user":
                c["user"] += 1
            elif t == "assistant":
                c["asst"] += 1
            u = obj.get("message", {}).get("usage", {})
            c["input"] += u.get("input_tokens", 0)
            c["output"] += u.get("output_tokens", 0)
            c["cache_read"] += u.get("cache_read_input_tokens", 0)
            c["cache_write"] += u.get("cache_creation_input_tokens", 0)
    if not ts:
        return None
    ts.sort()
    dts = [datetime.fromisoformat(x.replace("Z", "+00:00")) for x in ts]
    active = sum((b - a).total_seconds() for a, b in zip(dts, dts[1:])
                 if (b - a).total_seconds() <= 300)
    return dict(first=ts[0], last=ts[-1], active_secs=int(active), **c)


def scan_local(ledger):
    d = transcript_dir()
    if not os.path.isdir(d):
        print(f"(no local transcripts at {d} — rendering from the ledger only)",
              file=sys.stderr)
        return 0
    cutoff = ledger["baseline"]["cutoff"]
    added = 0
    for fname in sorted(os.listdir(d)):
        if not fname.endswith(".jsonl"):
            continue
        s = session_stats(os.path.join(d, fname))
        if s is None or s["first"][:10] <= cutoff:
            continue   # empty, or already inside the frozen baseline
        uuid = fname[: -len(".jsonl")]
        if uuid not in ledger["sessions"]:
            added += 1
        ledger["sessions"][uuid] = s
    return added


def totals(ledger):
    b = ledger["baseline"]
    t = dict(sessions=b["sessions"], user=b["user"], asst=b["asst"],
             input=b["input"], output=b["output"], cache_read=b["cache_read"],
             cache_write=b["cache_write"], active_secs=b["active_secs"])
    firsts, lasts = [b["first"]], [b["last"]]
    for s in ledger["sessions"].values():
        t["sessions"] += 1
        for k in ("user", "asst", "input", "output", "cache_read",
                  "cache_write", "active_secs"):
            t[k] += s[k]
        firsts.append(s["first"][:10])
        lasts.append(s["last"][:10])
    t["first"], t["last"] = min(firsts), max(lasts)
    return t


def render_block(t):
    active_min = t["active_secs"] // 60
    total = t["input"] + t["output"] + t["cache_read"] + t["cache_write"]
    opm = round(t["output"] / t["asst"]) if t["asst"] else 0
    return f"""<!-- devstats:start -->
This project was built entirely through a conversation with Claude Code, across
multiple machines. Numbers accumulate in a per-session ledger (`devstats.json`).

- **First message:** {t['first']}
- **Last message:** {t['last']}
- **Sessions:** {t['sessions']} — {t['user'] + t['asst']} messages ({t['user']} user + {t['asst']} assistant)
- **Active conversation time:** ~{active_min} min (~{active_min // 60}h {active_min % 60}m)

*Active time: sum of consecutive gaps ≤ 5 min within each session; cumulative and cross-machine.*

| Metric | Tokens |
|---|---:|
| Input (non-cache) | {t['input']:,} |
| Output | {t['output']:,} |
| Cache write | {t['cache_write']:,} |
| Cache read | {t['cache_read']:,} |
| **Total** | **~{total // 1_000_000} M** |

The assistant averaged **{opm} output tokens per message**. The early sessions ran with caveman mode — a Claude Code skill that strips filler while keeping full technical content — so this average blends those with later, prose-heavier sessions.
<!-- devstats:end -->"""


def update_readme(block):
    with open(README) as f:
        content = f.read()
    if "<!-- devstats:start -->" not in content:
        print("ERROR: devstats markers not found in README.md", file=sys.stderr)
        sys.exit(1)
    updated = re.sub(r"<!-- devstats:start -->.*?<!-- devstats:end -->",
                     block, content, flags=re.DOTALL)
    if updated != content:
        with open(README, "w") as f:
            f.write(updated)
        print("README.md updated.")
    else:
        print("README.md already up to date.")


def main():
    with open(LEDGER) as f:
        ledger = json.load(f)
    ledger.setdefault("sessions", {})
    added = scan_local(ledger)
    with open(LEDGER, "w") as f:
        json.dump(ledger, f, indent=2)
        f.write("\n")
    t = totals(ledger)
    update_readme(render_block(t))
    print(f"Ledger: {t['sessions']} sessions "
          f"({len(ledger['sessions'])} tracked + baseline), {added} new this run.")


if __name__ == "__main__":
    main()
