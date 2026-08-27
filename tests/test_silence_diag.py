"""Tests for silence_diag.py — telling a mute meter from a frozen one, and
naming the filter that swallowed the message.

The tool exists because two different failures look identical from the outside
(no Telegram message): a meter that stopped publishing, whose OFFLINE alarm may
never fire or never reach anyone, and a meter that keeps publishing one frozen
value, which nothing in the bot detects at all. These pin the decisions the
report is built on, plus the read-only promise in the docstring.
"""
import sqlite3
import time

import pytest

import silence_diag as sd


NOW = 1_700_000_000


def _rows(pairs):
    """Newest-first (ts, value) rows, the shape read_history returns."""
    return [{"ts": ts, "value": v} for ts, v in pairs]


@pytest.fixture
def diag_db(tmp_path):
    """A DB with a frozen meter, a mute meter, and an archived history."""
    dbfile = tmp_path / "sensors.db"
    con = sqlite3.connect(str(dbfile))
    con.executescript("""
        CREATE TABLE readings (id INTEGER PRIMARY KEY, sensor TEXT, value REAL, ts INTEGER);
        CREATE TABLE readings_archive (id INTEGER PRIMARY KEY, sensor TEXT, value REAL, ts INTEGER);
    """)
    rows = [("CDZ1_I", 0.0, NOW - i * 60) for i in range(100)]          # frozen
    rows += [("CDZ2_I", 3.0 + i % 4, NOW - 172800 - i * 60) for i in range(100)]  # mute
    con.executemany("INSERT INTO readings(sensor,value,ts) VALUES (?,?,?)", rows)
    con.executemany("INSERT INTO readings_archive(sensor,value,ts) VALUES (?,?,?)",
                    [("CDZ1_I", 4.2, NOW - 100 * 60 - 60)])
    con.commit()
    con.close()
    return str(dbfile)


def test_the_db_is_opened_read_only(diag_db):
    # the whole tool runs against production data: it must be unable to write
    con = sd.ro_conn(diag_db)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO readings(sensor,value,ts) VALUES ('x',1,1)")
    con.close()


def test_history_reaches_into_the_archive(diag_db):
    # a freeze is easily older than retention, so the archive has to be read too
    con = sd.ro_conn(diag_db)
    hist = sd.read_history(con, "CDZ1_I", 0, 20000)
    con.close()
    assert len(hist) == 101
    assert hist[0]["ts"] > hist[-1]["ts"]          # newest first
    assert hist[-1]["value"] == 4.2               # the archived row


def test_a_frozen_run_is_measured_from_the_last_change():
    a = sd.analyse(_rows([(NOW - i * 60, 0.0) for i in range(10)]
                         + [(NOW - 600, 2.5)]))
    assert a["changed_ts"] == NOW - 600
    assert a["frozen_for"] == 600                  # last change → newest reading
    assert a["frozen_capped"] is False


def test_a_never_changing_window_reports_a_lower_bound():
    # with no change inside the window the real freeze is longer than measured;
    # the flag is what stops the report from stating a length it cannot know
    a = sd.analyse(_rows([(NOW - i * 60, 0.0) for i in range(10)]))
    assert a["changed_ts"] is None
    assert a["frozen_capped"] is True
    assert a["frozen_for"] == 9 * 60


def test_cadence_is_the_median_gap_not_the_worst():
    # one long outage must not turn a 60 s meter into a 40 min one, or the mute
    # threshold below (3 x cadence) would never trip
    a = sd.analyse(_rows([(NOW, 1.0), (NOW - 60, 2.0), (NOW - 120, 3.0),
                          (NOW - 2520, 4.0)]))
    assert a["typ_gap"] == 60
    assert a["max_gap"] == 2400


def test_mute_uses_the_configured_interval_when_there_is_one():
    a = sd.analyse(_rows([(NOW - 3600 - i * 60, 1.0 + i) for i in range(5)]))
    assert sd.verdict(a, 180, now=NOW).startswith("MUTE")


def test_mute_falls_back_to_the_measured_cadence_without_a_config():
    # run outside the deploy there is no `interval`; a 60 s meter silent for an
    # hour is still mute, and saying so is the whole point of the fallback
    a = sd.analyse(_rows([(NOW - 3600 - i * 60, 1.0 + i) for i in range(5)]))
    v = sd.verdict(a, None, now=NOW)
    assert v.startswith("MUTE") and "measured cadence" in v


def test_a_publishing_meter_stuck_on_one_value_reads_as_frozen():
    a = sd.analyse(_rows([(NOW - i * 60, 0.0) for i in range(10)]))
    assert sd.verdict(a, 180, now=NOW).startswith("FROZEN")


def test_a_live_changing_meter_is_ok():
    a = sd.analyse(_rows([(NOW - i * 60, float(i)) for i in range(10)]))
    assert sd.verdict(a, 180, now=NOW).startswith("OK")


class _Grp:
    id, info, fields = "R2", "", ["CDZ1_I", "CDZ2_I"]
    below, for_seconds, repeat_seconds, stale_after = 1.0, 0, 3600, 180


def test_classification_matches_the_alarm_managers_order_of_tests():
    g = _Grp()
    assert sd.classify(0.0, 60, g, True) == "DARK"
    assert sd.classify(4.2, 60, g, True) == "LIT"
    # staleness wins over the value: an old zero proves nothing
    assert sd.classify(0.0, 3600, g, True) == "STALE"
    # an out-of-range sample carries no evidence either, exactly as for thresholds
    assert sd.classify(0.0, 60, g, False) == "INVALID"


def test_group_verdict_holds_on_stale_and_ends_only_on_lit():
    assert sd.group_verdict(["DARK", "DARK"]).startswith("ALL DARK")
    assert "POWERED" in sd.group_verdict(["DARK", "LIT"])
    # the case that produces silence, and the reason the user sees no message
    assert "HOLD" in sd.group_verdict(["DARK", "STALE"])
    assert "HOLD" in sd.group_verdict(["MISSING", "INVALID"])


def test_a_dm_needs_access_registration_and_a_subscription_on_that_key():
    registered = {1, 2}
    subs = {1: {"CDZ1_I"}, 2: {"R2"}, 3: {"CDZ1_I"}}
    # user 2 follows the blackout group but not the field: no offline DM
    assert sd.dm_targets({1, 2}, registered, subs, "CDZ1_I") == {1}
    # user 3 is subscribed but never registered a DM
    assert sd.dm_targets({3}, registered, subs, "CDZ1_I") == set()
    # the group id is delivered on its own key
    assert sd.dm_targets({1, 2}, registered, subs, "R2") == {2}


def test_no_recipient_is_reported_as_an_empty_set_not_an_error():
    assert sd.dm_targets(set(), {1}, {1: {"CDZ1_I"}}, "CDZ1_I") == set()
