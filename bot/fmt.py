"""Shared value/duration formatting used outside the Telegram layer.

`telegram_bot._fmt_ago` is deliberately coarse (a 1h23m outage reads "1h") and
`_fmt_uptime` floors at minutes (a 45s outage reads "0m"); neither is importable
from `alarm_manager` without pulling the whole Telegram surface in. Blackout
messages need a duration that stays honest at both ends of the scale, so it
lives here.
"""


def fmt_duration(secs: float) -> str:
    """Human duration with the two most significant units: 45s, 12m 30s, 1h 23m, 2d 4h."""
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        m, s = divmod(secs, 60)
        return f"{m}m {s}s" if s else f"{m}m"
    if secs < 86400:
        h, r = divmod(secs, 3600)
        m = r // 60
        return f"{h}h {m}m" if m else f"{h}h"
    d, r = divmod(secs, 86400)
    h = r // 3600
    return f"{d}d {h}h" if h else f"{d}d"
