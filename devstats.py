#!/usr/bin/env python3
"""
Extract development effort stats from Claude Code session logs and update README.md.
Run before git push or manually: python3 devstats.py
"""
import json
import os
import re
import sys
from datetime import datetime

PROJECT_DIR = os.path.expanduser("~/.claude/projects/-Users-ghedo-script-AllClaude-teams")
README = os.path.join(os.path.dirname(__file__), "README.md")

def load_stats():
    timestamps = []
    totals = dict(user=0, asst=0, input=0, output=0, cache_read=0, cache_write=0)

    for fname in os.listdir(PROJECT_DIR):
        if not fname.endswith(".jsonl"):
            continue
        with open(os.path.join(PROJECT_DIR, fname)) as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                ts = obj.get("timestamp")
                if ts:
                    timestamps.append(ts)
                t = obj.get("type", "")
                if t == "user":
                    totals["user"] += 1
                elif t == "assistant":
                    totals["asst"] += 1
                u = obj.get("message", {}).get("usage", {})
                totals["input"] += u.get("input_tokens", 0)
                totals["output"] += u.get("output_tokens", 0)
                totals["cache_read"] += u.get("cache_read_input_tokens", 0)
                totals["cache_write"] += u.get("cache_creation_input_tokens", 0)

    if not timestamps:
        print("No session logs found.", file=sys.stderr)
        sys.exit(1)

    timestamps.sort()
    dts = [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in timestamps]
    active_secs = sum(
        (b - a).total_seconds()
        for a, b in zip(dts, dts[1:])
        if (b - a).total_seconds() <= 300
    )
    active_min = int(active_secs / 60)
    sessions = len([f for f in os.listdir(PROJECT_DIR) if f.endswith(".jsonl")])

    return dict(
        first=timestamps[0][:10],
        last=timestamps[-1][:10],
        sessions=sessions,
        user=totals["user"],
        asst=totals["asst"],
        messages=totals["user"] + totals["asst"],
        active_min=active_min,
        active_h=active_min // 60,
        active_m=active_min % 60,
        input=totals["input"],
        output=totals["output"],
        cache_read=totals["cache_read"],
        cache_write=totals["cache_write"],
        output_per_msg=round(totals["output"] / totals["asst"]) if totals["asst"] else 0,
    )


def render_block(s):
    return f"""\
<!-- devstats:start -->
This project was built entirely through a conversation with Claude Code.
Numbers extracted from local session transcripts.

- **First message:** {s['first']}
- **Last message:** {s['last']}
- **Sessions:** {s['sessions']} — {s['messages']} messages ({s['user']} user + {s['asst']} assistant)
- **Active conversation time:** ~{s['active_min']} min (~{s['active_h']}h {s['active_m']}m)

*Active time: sum of consecutive gaps ≤ 5 min across all sessions. Longer gaps discarded.*

| Metric | Tokens |
|---|---:|
| Input (non-cache) | {s['input']:,} |
| Output | {s['output']:,} |
| Cache write | {s['cache_write']:,} |
| Cache read | {s['cache_read']:,} |
| **Total** | **~{(s['input']+s['output']+s['cache_read']+s['cache_write'])//1_000_000} M** |

### Caveman mode

All {s['sessions']} sessions ran with caveman mode active — a Claude Code skill that drops filler words, articles, and pleasantries from assistant responses while keeping full technical content. The assistant produced an average of **{s['output_per_msg']} output tokens per message**. The saving is modest compared to prose-heavy projects because the dominant output here is code, which caveman leaves untouched.
<!-- devstats:end -->"""


def update_readme(block):
    with open(README) as f:
        content = f.read()
    if not re.search(r"<!-- devstats:start -->", content):
        print("ERROR: devstats markers not found in README.md", file=sys.stderr)
        sys.exit(1)
    updated = re.sub(
        r"<!-- devstats:start -->.*?<!-- devstats:end -->",
        block,
        content,
        flags=re.DOTALL,
    )
    if updated != content:
        with open(README, "w") as f:
            f.write(updated)
        print("README.md updated.")
    else:
        print("README.md already up to date.")


if __name__ == "__main__":
    s = load_stats()
    block = render_block(s)
    update_readme(block)
