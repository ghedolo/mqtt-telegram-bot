"""Tests for bot.alarm_manager — threshold repeat gating, offline detection,
and the blackout DARK/LIT/UNKNOWN state machine (the partial-outage
no-false-recovery case is the important one).

Time is driven by monkeypatching alarm_manager.time.time; coroutines are run
with asyncio.run so no pytest-asyncio plugin is needed.
"""
import asyncio

import pytest

from bot import alarm_manager as am_mod
from bot.config import DeviceConfig, SensorConfig, BlackoutGroup


def fmt(sensor, value):
    return f"{value:.1f}"


class Rec:
    """Async notifier stub that records (key, message) calls."""
    def __init__(self):
        self.msgs = []

    async def __call__(self, key, msg):
        self.msgs.append((key, msg))


@pytest.fixture
def clock(monkeypatch):
    holder = {"t": 1000}
    monkeypatch.setattr(am_mod.time, "time", lambda: holder["t"])
    return holder


def make_device(key="D", topic="t/d", interval=10):
    sc = SensorConfig(
        name=f"{key}_T", topic=topic, json_path=None, interval=interval,
        info="", unit="", default_alarm_high=None, default_alarm_low=None,
    )
    return DeviceConfig(key=key, topic=topic, interval=interval,
                        info="", note="", fields={"T": sc})


# --- threshold ---

def test_threshold_raise_gate_repeat_recover(temp_db, clock):
    rec = Rec()
    am = am_mod.AlarmManager(720, 3600, rec, Rec(), fmt)
    temp_db.set_threshold("A_T", 30.0)

    asyncio.run(am.check_threshold("A_T", 35.0))
    assert len(rec.msgs) == 1
    assert rec.msgs[0][1].startswith("🔴")

    # still over, but within the repeat window -> no new alarm
    asyncio.run(am.check_threshold("A_T", 36.0))
    assert len(rec.msgs) == 1

    # past the repeat interval -> alarm repeats
    clock["t"] += 720
    asyncio.run(am.check_threshold("A_T", 36.0))
    assert len(rec.msgs) == 2

    # back within range -> single recovery, state resets
    asyncio.run(am.check_threshold("A_T", 20.0))
    assert len(rec.msgs) == 3
    assert rec.msgs[2][1].startswith("🟢")


def test_threshold_low(temp_db, clock):
    rec = Rec()
    am = am_mod.AlarmManager(720, 3600, rec, Rec(), fmt)
    temp_db.set_threshold_low("A_T", 10.0)

    asyncio.run(am.check_threshold_low("A_T", 5.0))
    assert len(rec.msgs) == 1 and rec.msgs[0][1].startswith("🔴")

    asyncio.run(am.check_threshold_low("A_T", 15.0))
    assert len(rec.msgs) == 2 and rec.msgs[1][1].startswith("🟢")


def test_threshold_none_set_no_alarm(temp_db, clock):
    rec = Rec()
    am = am_mod.AlarmManager(720, 3600, rec, Rec(), fmt)
    asyncio.run(am.check_threshold("A_T", 999.0))  # no threshold configured
    assert rec.msgs == []


# --- offline ---

def test_offline_then_recovery(temp_db, clock):
    recdev = Rec()
    am = am_mod.AlarmManager(720, 3600, Rec(), recdev, fmt)
    am._started_at = clock["t"] - 1000     # past the startup grace
    dev = make_device(interval=10)         # offline_after = 30s

    asyncio.run(am.check_offline(dev))     # no data ever seen -> offline
    assert len(recdev.msgs) == 1
    assert "OFFLINE" in recdev.msgs[0][1]

    am.record_topic_message("t/d")         # fresh message arrives
    asyncio.run(am.check_offline(dev))
    assert len(recdev.msgs) == 2
    assert "ONLINE" in recdev.msgs[1][1]


def test_offline_suppressed_during_startup_grace(temp_db, clock):
    recdev = Rec()
    am = am_mod.AlarmManager(720, 3600, Rec(), recdev, fmt)
    # just started: (now - started_at) < offline_after -> no offline alarm yet
    am._started_at = clock["t"]
    dev = make_device(interval=10)
    asyncio.run(am.check_offline(dev))
    assert recdev.msgs == []


# --- blackout state machine ---

def _group():
    return BlackoutGroup(
        id="R2", info="R2", fields=["X_I1", "X_I2"],
        below=0.5, for_seconds=10, repeat_seconds=3600, stale_after=15,
    )


def test_blackout_not_raised_until_sustained(temp_db, clock):
    recbo = Rec()
    g = _group()
    am = am_mod.AlarmManager(720, 3600, Rec(), Rec(), fmt, recbo, {"R2": g})

    temp_db.insert_reading("X_I1", 0.0, ts=1000)
    temp_db.insert_reading("X_I2", 0.0, ts=1000)
    asyncio.run(am.check_blackout(g))
    assert recbo.msgs == []          # all-dark but 0s < for_seconds


def test_blackout_lifecycle_raise_hold_end(temp_db, clock):
    recbo = Rec()
    g = _group()
    am = am_mod.AlarmManager(720, 3600, Rec(), Rec(), fmt, recbo, {"R2": g})

    # t=1000: both dark & fresh -> starts the sustain timer, no alarm yet
    temp_db.insert_reading("X_I1", 0.0, ts=1000)
    temp_db.insert_reading("X_I2", 0.0, ts=1000)
    asyncio.run(am.check_blackout(g))
    assert recbo.msgs == []

    # t=1011: still all-dark, sustained >= 10s -> RAISE
    clock["t"] = 1011
    temp_db.insert_reading("X_I1", 0.0, ts=1011)
    temp_db.insert_reading("X_I2", 0.0, ts=1011)
    asyncio.run(am.check_blackout(g))
    assert len(recbo.msgs) == 1 and recbo.msgs[0][1].startswith("⚡")
    assert am._state("R2", "blackout").active is True

    # t=1100: one meter dies (X_I2 stale), the other still dark & fresh.
    # Partial outage -> HOLD, no false recovery, alarm stays active.
    clock["t"] = 1100
    temp_db.insert_reading("X_I1", 0.0, ts=1100)     # fresh DARK
    # X_I2 last reading is ts=1011 -> age 89 > stale_after 15 -> UNKNOWN
    asyncio.run(am.check_blackout(g))
    assert len(recbo.msgs) == 1                       # no new message
    assert am._state("R2", "blackout").active is True

    # t=1200: a field reads LIT (current back) -> confirmed END
    clock["t"] = 1200
    temp_db.insert_reading("X_I1", 2.0, ts=1200)      # LIT
    asyncio.run(am.check_blackout(g))
    assert len(recbo.msgs) == 2 and recbo.msgs[1][1].startswith("🔌")
    assert am._state("R2", "blackout").active is False
    # recovery reset the sustain timer so a new outage restarts cleanly
    assert am._state("R2", "blackout").since == 0


def test_blackout_for_seconds_zero_raises_immediately(temp_db, clock):
    recbo = Rec()
    g = BlackoutGroup(id="R2", info="R2", fields=["X_I1"], below=0.5,
                      for_seconds=0, repeat_seconds=3600, stale_after=15)
    am = am_mod.AlarmManager(720, 3600, Rec(), Rec(), fmt, recbo, {"R2": g})
    temp_db.insert_reading("X_I1", 0.0, ts=1000)      # fresh & dark
    asyncio.run(am.check_blackout(g))
    assert len(recbo.msgs) == 1 and recbo.msgs[0][1].startswith("⚡")


def test_blackout_reads_signal_cache_without_db(temp_db, clock):
    # A signal-backed field has no DB rows: check_blackout must classify it
    # from the in-memory cache (record_signal), routing cache-or-DB.
    recbo = Rec()
    g = BlackoutGroup(id="SIG", info="SIG", fields=["X_IF"], below=0.5,
                      for_seconds=0, repeat_seconds=3600, stale_after=15)
    am = am_mod.AlarmManager(720, 3600, Rec(), Rec(), fmt, recbo, {"SIG": g})

    am.record_signal("X_IF", 0.0)                     # fresh & dark, in memory only
    asyncio.run(am.check_blackout(g))
    assert len(recbo.msgs) == 1 and recbo.msgs[0][1].startswith("⚡")
    assert temp_db.get_latest("X_IF") is None         # never stored
    assert am.signal_snapshot()["X_IF"]["value"] == 0.0

    # a LIT signal value confirms recovery from the cache too
    clock["t"] = 1005
    am.record_signal("X_IF", 2.0)
    asyncio.run(am.check_blackout(g))
    assert len(recbo.msgs) == 2 and recbo.msgs[1][1].startswith("🔌")


def test_blackout_repeat_notification(temp_db, clock):
    recbo = Rec()
    g = _group()
    am = am_mod.AlarmManager(720, 3600, Rec(), Rec(), fmt, recbo, {"R2": g})
    # raise
    temp_db.insert_reading("X_I1", 0.0, ts=1000)
    temp_db.insert_reading("X_I2", 0.0, ts=1000)
    asyncio.run(am.check_blackout(g))
    clock["t"] = 1011
    temp_db.insert_reading("X_I1", 0.0, ts=1011)
    temp_db.insert_reading("X_I2", 0.0, ts=1011)
    asyncio.run(am.check_blackout(g))
    assert len(recbo.msgs) == 1

    # still dark, but within repeat_seconds -> no repeat
    clock["t"] = 1100
    temp_db.insert_reading("X_I1", 0.0, ts=1100)
    temp_db.insert_reading("X_I2", 0.0, ts=1100)
    asyncio.run(am.check_blackout(g))
    assert len(recbo.msgs) == 1

    # past repeat_seconds (3600) -> repeat "still no current"
    clock["t"] = 1011 + 3600 + 1
    ts = clock["t"]
    temp_db.insert_reading("X_I1", 0.0, ts=ts)
    temp_db.insert_reading("X_I2", 0.0, ts=ts)
    asyncio.run(am.check_blackout(g))
    assert len(recbo.msgs) == 2 and "still no current" in recbo.msgs[1][1]


def test_blackout_all_stale_never_raises(temp_db, clock):
    recbo = Rec()
    g = _group()
    am = am_mod.AlarmManager(720, 3600, Rec(), Rec(), fmt, recbo, {"R2": g})
    # readings exist but are older than stale_after -> all UNKNOWN, not DARK
    temp_db.insert_reading("X_I1", 0.0, ts=900)   # age 100 > 15
    temp_db.insert_reading("X_I2", 0.0, ts=900)
    asyncio.run(am.check_blackout(g))
    assert recbo.msgs == []
    assert am._state("R2", "blackout").active is False


def test_check_blackout_for_dispatches_only_watching_groups(temp_db, clock):
    recbo = Rec()
    ga = BlackoutGroup(id="A", info="A", fields=["X_I"], below=0.5,
                       for_seconds=0, repeat_seconds=3600, stale_after=15)
    gb = BlackoutGroup(id="B", info="B", fields=["Y_I"], below=0.5,
                       for_seconds=0, repeat_seconds=3600, stale_after=15)
    am = am_mod.AlarmManager(720, 3600, Rec(), Rec(), fmt, recbo,
                             {"A": ga, "B": gb})
    # both groups could raise (both fields dark & fresh)
    temp_db.insert_reading("X_I", 0.0, ts=1000)
    temp_db.insert_reading("Y_I", 0.0, ts=1000)
    # event on X_I must evaluate only group A
    asyncio.run(am.check_blackout_for("X_I"))
    assert [gid for gid, _ in recbo.msgs] == ["A"]
    assert "B:blackout" not in am._states


def test_blackout_notify_none_is_noop(temp_db, clock):
    g = _group()
    # no notify_blackout_fn -> check_blackout returns early, no crash/state
    am = am_mod.AlarmManager(720, 3600, Rec(), Rec(), fmt)
    temp_db.insert_reading("X_I1", 0.0, ts=1000)
    temp_db.insert_reading("X_I2", 0.0, ts=1000)
    asyncio.run(am.check_blackout(g))
    assert "R2:blackout" not in am._states
