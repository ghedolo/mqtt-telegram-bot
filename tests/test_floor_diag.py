"""Tests for floor_diag.py — is a low current the meter's zero-offset, or a load?

A blackout message can show `SM1_CDZ2_IF=0.4` while the field is classified
DARK, and the number alone does not say whether the meter is reporting an
offset with no current flowing (detection correct) or a genuine residual load
(the group's `below` measuring the wrong thing). The tool answers that from the
recorded history; these tests pin the three decisions it is built on — the
classification itself, the outage windows it cross-checks against, and the
read-only promise in the docstring.
"""
import sqlite3

import pytest

import floor_diag as fd


NOW = 1_700_000_000
STEP = 0.1                      # one storage step at the default 1 decimal


@pytest.fixture
def floor_db(tmp_path):
    """A DB with an archived tail, so history older than retention is reachable."""
    dbfile = tmp_path / "sensors.db"
    con = sqlite3.connect(str(dbfile))
    con.executescript("""
        CREATE TABLE readings (id INTEGER PRIMARY KEY, sensor TEXT, value REAL, ts INTEGER);
        CREATE TABLE readings_archive (id INTEGER PRIMARY KEY, sensor TEXT, value REAL, ts INTEGER);
        CREATE TABLE alarms (id INTEGER PRIMARY KEY, sensor TEXT, kind TEXT,
                             message TEXT, ts INTEGER);
    """)
    con.executemany("INSERT INTO readings(sensor,value,ts) VALUES (?,?,?)",
                    [("CDZ1_I", 2.0, NOW - i * 60) for i in range(10)])
    con.executemany("INSERT INTO readings_archive(sensor,value,ts) VALUES (?,?,?)",
                    [("CDZ1_I", 0.4, NOW - 100_000)])
    con.commit()
    con.close()
    return str(dbfile)


def test_the_db_is_opened_read_only(floor_db):
    # the tool runs against production data: it must be unable to write
    con = fd.ro_conn(floor_db)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO readings(sensor,value,ts) VALUES ('x',1,1)")
    con.close()


def test_history_reaches_into_the_archive(floor_db):
    # the floor is a long-run property; most of the evidence predates retention
    con = fd.ro_conn(floor_db)
    rows = fd.read_history(con, "CDZ1_I", 0)
    con.close()
    assert len(rows) == 11
    assert rows[0]["ts"] < rows[-1]["ts"]        # oldest first
    assert rows[0]["value"] == 0.4               # the archived row


def test_history_survives_a_db_without_the_archive_table(tmp_path):
    dbfile = tmp_path / "no_archive.db"
    con = sqlite3.connect(str(dbfile))
    con.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY, sensor TEXT, "
                "value REAL, ts INTEGER)")
    con.execute("INSERT INTO readings(sensor,value,ts) VALUES ('CDZ1_I',0.4,1)")
    con.commit()
    con.close()
    con = fd.ro_conn(str(dbfile))
    assert len(fd.read_history(con, "CDZ1_I", 0)) == 1
    con.close()


# ------------------------------------------------------------------ classify

def test_a_meter_that_never_reads_zero_has_a_floor():
    # 0.4 on every dark sample and nothing lower: that is the offset, not a load
    vals = [0.4] * 20 + [2.0] * 100
    c = fd.classify(vals, low_cut=0.5, step=STEP)
    assert c["kind"] == "floor"
    assert (c["low_min"], c["low_max"]) == (0.4, 0.4)


def test_a_meter_that_does_read_zero_makes_a_low_value_a_real_load():
    # the meter can express zero, so the 0.4 samples are current that flowed
    vals = [0.0] * 10 + [0.4] * 5 + [3.0] * 100
    c = fd.classify(vals, low_cut=0.5, step=STEP)
    assert c["kind"] == "load"
    assert c["nonzero_low"] == [0.4] * 5


def test_a_low_tail_that_is_all_zero_is_genuine_zero_current():
    vals = [0.0] * 10 + [3.0] * 100
    assert fd.classify(vals, low_cut=0.5, step=STEP)["kind"] == "zero"


def test_a_spread_low_tail_without_zero_is_not_separable():
    # 0.1 .. 0.45 is wider than two storage steps: offset and small load mixed
    vals = [0.1, 0.2, 0.3, 0.45] + [3.0] * 100
    assert fd.classify(vals, low_cut=0.5, step=STEP)["kind"] == "mixed"


def test_a_field_that_never_goes_dark_is_named_as_such():
    # such a field can never make the group all-DARK; it only blocks detection
    c = fd.classify([2.0, 3.0, 2.5], low_cut=0.5, step=STEP)
    assert c["kind"] == "never-dark"
    assert c["low_n"] == 0


def test_the_floor_verdict_tolerates_one_storage_step_of_jitter():
    # 0.4/0.5 is quantisation of the same offset, not two different states
    vals = [0.4, 0.5, 0.4] + [2.0] * 50
    assert fd.classify(vals, low_cut=0.6, step=STEP)["kind"] == "floor"


def test_a_single_low_reading_is_not_enough_for_a_verdict():
    # production case: 1 sample below 0.5 A in 78k readings. One sample is
    # always a "tight cluster", so the floor test would pass on no evidence.
    vals = [0.44] + [11.6] * 500
    c = fd.classify(vals, low_cut=0.5, step=0.01)
    assert c["kind"] == "thin"
    assert c["low_n"] == 1


def test_enough_low_readings_turn_the_same_shape_into_a_floor():
    vals = [0.44] * 3 + [11.6] * 500
    assert fd.classify(vals, low_cut=0.5, step=0.01)["kind"] == "floor"


# -------------------------------------------------- distance to an outage

def test_a_reading_inside_a_window_is_at_distance_zero():
    assert fd.window_distance(120, [(100, 160)], now=1000) == 0


def test_the_sample_after_a_short_outage_is_measured_from_its_end():
    # a 60 s meter takes no sample inside a 5 s outage: the reading that follows
    # is the outage's own tail, and only its distance says so
    assert fd.window_distance(200, [(100, 160)], now=1000) == 40


def test_distance_is_taken_to_the_nearest_of_several_windows():
    assert fd.window_distance(480, [(100, 160), (500, 900)], now=1000) == 20


def test_an_open_window_runs_to_now():
    assert fd.window_distance(2000, [(100, None)], now=5000) == 0


def test_no_recorded_window_yields_no_distance():
    assert fd.window_distance(200, [], now=1000) is None


# ------------------------------------------------------- outage windows

def _alarm_con(rows):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE alarms (id INTEGER PRIMARY KEY, sensor TEXT, "
                "kind TEXT, message TEXT, ts INTEGER)")
    con.executemany("INSERT INTO alarms(sensor,kind,message,ts) VALUES (?,?,?,?)",
                    [(s, k, "m", t) for s, k, t in rows])
    return con


def test_windows_pair_a_blackout_with_its_end():
    con = _alarm_con([("R2", "BLACKOUT", 100), ("R2", "BLACKOUT_END", 160)])
    assert fd.outage_windows(con, "R2") == [(100, 160)]


def test_repeat_rows_do_not_open_a_second_window():
    # a persisting blackout writes further BLACKOUT rows; the outage is still one
    con = _alarm_con([("R2", "BLACKOUT", 100), ("R2", "BLACKOUT", 200),
                      ("R2", "BLACKOUT_END", 300)])
    assert fd.outage_windows(con, "R2") == [(100, 300)]


def test_an_unclosed_blackout_stays_open():
    con = _alarm_con([("R2", "BLACKOUT", 100)])
    assert fd.outage_windows(con, "R2") == [(100, None)]
    assert fd.in_any(150, [(100, None)], now=500) is True
    assert fd.in_any(600, [(100, None)], now=500) is False


def test_windows_are_per_group():
    con = _alarm_con([("R2", "BLACKOUT", 100), ("R2", "BLACKOUT_END", 160),
                      ("R3", "BLACKOUT", 500), ("R3", "BLACKOUT_END", 900)])
    assert fd.outage_windows(con, "R3") == [(500, 900)]


def test_a_reading_outside_every_window_is_not_outage_evidence():
    wins = [(100, 160), (500, 900)]
    assert fd.in_any(120, wins, now=1000) is True
    assert fd.in_any(300, wins, now=1000) is False


# ------------------------------------------------------------- gap shape

def test_the_empty_interval_separates_the_offset_from_the_working_range():
    gap, lo, hi = fd.biggest_gap([0.4, 0.4, 1.7, 2.0], upto=2.0)
    assert (lo, hi) == (0.4, 1.7)
    assert round(gap, 3) == 1.3


def test_a_single_distinct_value_has_no_gap_to_report():
    assert fd.biggest_gap([0.4, 0.4, 0.4], upto=2.0) is None
