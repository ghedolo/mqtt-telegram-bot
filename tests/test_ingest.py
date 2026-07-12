"""Integration test for bot.ingest.process_reading — the MQTT reading path
wired to the real DB and a real AlarmManager (only the notifiers are stubs).

Verifies the glue in one flow: rounding-before-storage, out-of-range readings
stored but not alarmed, threshold alarm end-to-end, and blackout re-evaluation.
"""
import asyncio

import pytest

from bot import config
from bot import alarm_manager as am_mod
from bot.ingest import process_reading


CREDS = """
telegram:
  token: "T"
  group_id: -100
mqtt:
  host: "broker"
  port: 1883
groups:
  ops: [1]
"""

DEFAULTS = """
defaults:
  interval: 300
devices:
  SM1:
    topic: "t/sm1"
    viewers: [ops]
    fields:
      T:
        decimals: 1
        validMax: 80
      I:
        decimals: 2
blackouts:
  R2:
    fields: [SM1_I]
    below: 0.5
    for_seconds: 0
    stale_after: 15
"""


class Rec:
    def __init__(self):
        self.msgs = []

    async def __call__(self, key, msg):
        self.msgs.append((key, msg))


@pytest.fixture
def env(tmp_path, temp_db):
    sd = tmp_path / "sensors.d"
    sd.mkdir()
    (sd / "00-defaults.yaml").write_text(DEFAULTS)
    cf = tmp_path / "credentials.yaml"
    cf.write_text(CREDS)
    cfg = config.load(str(sd), str(cf))

    thr = Rec()
    blackout = Rec()
    alarms = am_mod.AlarmManager(
        720, 3600, thr, Rec(), cfg.fmt, blackout, cfg.blackouts,
    )
    return cfg, alarms, thr, blackout, temp_db


def test_reading_rounded_before_storage(env):
    cfg, alarms, thr, blackout, db = env
    asyncio.run(process_reading(cfg, alarms, "SM1_T", 21.47))   # decimals 1
    asyncio.run(process_reading(cfg, alarms, "SM1_I", 1.234))   # decimals 2
    assert db.get_latest("SM1_T")["value"] == 21.5
    assert db.get_latest("SM1_I")["value"] == 1.23


def test_out_of_range_stored_but_not_alarmed(env):
    cfg, alarms, thr, blackout, db = env
    db.set_threshold("SM1_T", 30.0)
    asyncio.run(process_reading(cfg, alarms, "SM1_T", 999.0))   # > validMax 80
    # stored...
    assert db.get_latest("SM1_T")["value"] == 999.0
    # ...but the glitch did not raise a threshold alarm
    assert thr.msgs == []


def test_threshold_alarm_end_to_end(env):
    cfg, alarms, thr, blackout, db = env
    db.set_threshold("SM1_T", 30.0)
    asyncio.run(process_reading(cfg, alarms, "SM1_T", 35.0))    # in range, over thr
    assert len(thr.msgs) == 1
    key, msg = thr.msgs[0]
    assert key == "SM1_T"
    assert msg.startswith("🔴")
    assert "35.0" in msg


def test_blackout_evaluated_on_reading(env):
    cfg, alarms, thr, blackout, db = env
    # R2 watches SM1_I, below 0.5, for_seconds 0 -> a dark reading raises at once
    asyncio.run(process_reading(cfg, alarms, "SM1_I", 0.0))
    assert len(blackout.msgs) == 1
    assert blackout.msgs[0][0] == "R2"
    assert blackout.msgs[0][1].startswith("⚡")
