"""Tests for cadence.py — measuring a sensor's real publish cadence.

The script exists to pick `interval`, the one setting offline detection reads
(`3 x interval`). A suggestion that is too low turns normal jitter into false
OFFLINE alarms; too high and a dead sensor goes unnoticed. These pin the two
things that decide that number, plus the read-only promise in the docstring.
"""
import sqlite3
import time

import pytest

import cadence
from bot import db as db_module


@pytest.fixture
def cadence_db(tmp_path, monkeypatch):
    """Readings for a 300 s probe and a 62 s meter, plus a 40 min probe outage."""
    dbfile = tmp_path / "sensors.db"
    monkeypatch.setattr(db_module, "DB_PATH", str(dbfile))
    db_module.init()
    now = int(time.time())
    rows = []
    for i in range(100):
        rows.append(("SM1_UTA1_T", 20.0, now - i * 300 - (2400 if i >= 50 else 0)))
    for i in range(100):
        rows.append(("SM1_CDZ1_I", 3.0, now - i * 62))
    rows.append(("SM1_UTA1_H", 50.0, now))          # a single reading: no gap to measure
    con = sqlite3.connect(str(dbfile))
    con.executemany("INSERT INTO readings(sensor,value,ts) VALUES (?,?,?)", rows)
    con.commit()
    con.close()
    return str(dbfile)


def test_a_62s_meter_is_not_rounded_to_a_whole_minute():
    # rounding 62 -> 120 would double the meter's OFFLINE delay (3 x 120 = 6 min
    # instead of 3.5) purely to make the number look tidy
    assert cadence._round_up(62) == 70
    assert cadence._round_up(60) == 60
    assert cadence._round_up(300.2) == 310
    assert cadence._round_up(3601) == 3660


def test_measured_cadence_matches_the_data(cadence_db):
    con = cadence._ro_conn(cadence_db)
    meter = cadence.measure(con, "SM1_CDZ1_I", 0, 2000)
    probe = cadence.measure(con, "SM1_UTA1_T", 0, 2000)
    con.close()
    assert meter["median"] == 62
    assert meter["max"] == 62
    assert probe["median"] == 300
    # the outage must survive as the worst gap — that is what the report flags
    assert probe["max"] == 2700


def test_a_single_reading_yields_no_cadence(cadence_db):
    con = cadence._ro_conn(cadence_db)
    stat = cadence.measure(con, "SM1_UTA1_H", 0, 2000)
    con.close()
    assert stat["samples"] == 1
    assert stat["median"] is None and stat["p90"] is None


def test_the_db_is_opened_read_only(cadence_db):
    # the docstring promises READ-ONLY; a diagnostic run against production must
    # not be able to mutate the readings it is measuring
    con = cadence._ro_conn(cadence_db)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO readings(sensor,value,ts) VALUES ('X',1.0,1)")
    con.close()


def test_patterns_filter_by_glob(cadence_db):
    con = cadence._ro_conn(cadence_db)
    assert cadence.sensor_names(con, ["SM1_UTA1_*"]) == ["SM1_UTA1_H", "SM1_UTA1_T"]
    assert cadence.sensor_names(con, []) == ["SM1_CDZ1_I", "SM1_UTA1_H", "SM1_UTA1_T"]
    assert cadence.sensor_names(con, ["NOPE_*"]) == []
    con.close()


def test_report_flags_a_gap_that_would_have_alarmed(cadence_db, capsys):
    con = cadence._ro_conn(cadence_db)
    stats = [cadence.measure(con, n, 0, 2000) for n in ("SM1_UTA1_T", "SM1_CDZ1_I")]
    con.close()
    cadence.report(stats, {})
    out = capsys.readouterr().out
    # the probe's 45 min outage is longer than 3 x 300 s, the meter's is not
    assert "SM1_UTA1_T: worst gap" in out
    assert "SM1_CDZ1_I: worst gap" not in out
