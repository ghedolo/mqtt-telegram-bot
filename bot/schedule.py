"""Wall-clock scheduling helpers.

Kept dependency-free (stdlib only) so the timing logic is unit-testable in
isolation, without importing the Telegram/MQTT stack. Used by the archive
loop to fire at a fixed daily time instead of a relative 24 h sleep — the
latter never elapses on a host that is powered off for part of the day.
"""
from datetime import datetime, timedelta


def next_occurrence(now: datetime, hour: int, minute: int) -> datetime:
    """The next datetime at hour:minute strictly after `now`.

    Rolls to tomorrow if that time today has already passed or is exactly now.
    """
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def seconds_until(now: datetime, hhmm: str) -> float:
    """Seconds from `now` until the next `HH:MM` occurrence."""
    hour, minute = (int(x) for x in hhmm.split(":"))
    return (next_occurrence(now, hour, minute) - now).total_seconds()
