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


class _Cfg:
    """Minimal stand-in for AppConfig: a group watching two Signals, each on a
    device that also owns a stored current — the production shape."""

    class _Dev:
        def __init__(self, key, fields, interval=300):
            self.key, self.fields, self.interval = key, fields, interval
            self.availability_topic = None

    class _Sc:
        def __init__(self, name, device_key, interval=300):
            self.name, self.device_key, self.interval = name, device_key, interval

    def __init__(self):
        self.blackouts = {"G": _Grp()}
        self.blackouts["G"].fields = ["CDZ1_IF", "CDZ2_IF"]
        self.signals = {"CDZ1_IF": object(), "CDZ2_IF": object()}
        self.sensors = {n: self._Sc(n, n.split("_")[0])
                        for n in ("CDZ1_I", "CDZ1_T", "CDZ2_I")}
        self.devices = {
            "CDZ1": self._Dev("CDZ1", {"I": self.sensors["CDZ1_I"],
                                       "T": self.sensors["CDZ1_T"]}),
            "CDZ2": self._Dev("CDZ2", {"I": self.sensors["CDZ2_I"]}),
        }
        self._owner = {"CDZ1_IF": "CDZ1", "CDZ2_IF": "CDZ2",
                       "CDZ1_I": "CDZ1", "CDZ1_T": "CDZ1", "CDZ2_I": "CDZ2"}

    def device_of(self, name):
        return self._owner.get(name, "")


def test_scope_pulls_in_the_stored_siblings_of_a_signal_only_group(diag_db):
    # a group watching only Signals has no history to read: without the
    # siblings the whole report comes out empty, which is what happened in
    # production on 2026-08-27
    con = sd.ro_conn(diag_db)
    watched = sd.scope(_Cfg(), [], con)
    con.close()
    assert [n for n, _, _ in watched] == [
        "CDZ1_IF", "CDZ2_IF", "CDZ1_I", "CDZ1_T", "CDZ2_I"]
    kinds = {n: k for n, k, _ in watched}
    assert kinds["CDZ1_IF"] == "signal" and kinds["CDZ1_I"] == "sensor"
    # siblings are tagged as belonging to the group, but marked as such
    assert dict((n, g) for n, _, g in watched)["CDZ2_I"] == "G*"


def test_scope_lists_each_field_once(diag_db):
    con = sd.ro_conn(diag_db)
    names = [n for n, _, _ in sd.scope(_Cfg(), ["CDZ*"], con)]
    con.close()
    assert len(names) == len(set(names))


@pytest.fixture
def health_db(tmp_path):
    """A DB whose newest reading is hours old — the bot stopped ingesting."""
    dbfile = tmp_path / "health.db"
    con = sqlite3.connect(str(dbfile))
    con.executescript("""
        CREATE TABLE readings (id INTEGER PRIMARY KEY, sensor TEXT, value REAL, ts INTEGER);
        CREATE TABLE alarms (id INTEGER PRIMARY KEY, sensor TEXT, kind TEXT, message TEXT, ts INTEGER);
    """)
    old = int(time.time()) - 6 * 3600
    con.executemany("INSERT INTO readings(sensor,value,ts) VALUES (?,?,?)",
                    [("CDZ1_I", 2.0, old - i * 300) for i in range(10)])
    con.execute("INSERT INTO alarms(sensor,kind,message,ts) VALUES "
                "('CDZ1','OFFLINE','OFFLINE CDZ1: still no data',?)", (old,))
    con.commit()
    con.close()
    return str(dbfile)


def test_health_flags_a_bot_that_stopped_ingesting(health_db, capsys):
    # the decisive distinction: repeats stop because nothing runs, not because
    # a device recovered. Both look the same in the alarm history.
    con = sd.ro_conn(health_db)
    sd.sec_health(None, con, [("CDZ1_I", "sensor", "")])
    con.close()
    out = capsys.readouterr().out
    assert "NOTHING was stored in the last hour" in out
    assert "no OFFLINE repeat and no ONLINE" in out


def test_health_stays_quiet_while_readings_keep_arriving(tmp_path, capsys):
    dbfile = tmp_path / "live.db"
    con = sqlite3.connect(str(dbfile))
    con.executescript("""
        CREATE TABLE readings (id INTEGER PRIMARY KEY, sensor TEXT, value REAL, ts INTEGER);
        CREATE TABLE alarms (id INTEGER PRIMARY KEY, sensor TEXT, kind TEXT, message TEXT, ts INTEGER);
    """)
    now = int(time.time())
    con.executemany("INSERT INTO readings(sensor,value,ts) VALUES (?,?,?)",
                    [("CDZ1_T", 21.0, now - i * 60) for i in range(30)])
    con.commit()
    con.close()
    ro = sd.ro_conn(str(dbfile))
    sd.sec_health(None, ro, [("CDZ1_T", "sensor", "")])
    ro.close()
    assert "NOTHING was stored" not in capsys.readouterr().out


def test_the_global_tail_shows_subjects_outside_the_examined_fields(health_db, capsys):
    # a bot-wide fault (dropped MQTT session) and a single dead meter look the
    # same in section E, which is scoped to the fields being examined
    con = sd.ro_conn(health_db)
    con.close()
    rw = sqlite3.connect(health_db)
    now = int(time.time())
    rw.executemany("INSERT INTO alarms(sensor,kind,message,ts) VALUES (?,?,?,?)",
                   [("OTHER_DEV", "OFFLINE", "OFFLINE OTHER_DEV: no data for >900s", now - 60),
                    ("THIRD_DEV", "OFFLINE", "OFFLINE THIRD_DEV: no data for >900s", now - 60)])
    rw.commit()
    rw.close()
    con = sd.ro_conn(health_db)
    sd.sec_all_alarms(con, 10)
    con.close()
    out = capsys.readouterr().out
    assert "OTHER_DEV" in out and "THIRD_DEV" in out
    assert "same minute" in out


def test_the_global_tail_honours_its_limit(health_db, capsys):
    con = sd.ro_conn(health_db)
    sd.sec_all_alarms(con, 1)
    con.close()
    body = [l for l in capsys.readouterr().out.splitlines() if "still no data" in l]
    assert len(body) == 1


def test_recipients_section_dates_the_dm_registration(tmp_path, capsys, monkeypatch):
    # the tables hold today's state: someone who registered after the outage was
    # not a recipient when it happened, and the report must make that checkable
    dbfile = tmp_path / "who.db"
    con = sqlite3.connect(str(dbfile))
    con.executescript("""
        CREATE TABLE readings (id INTEGER PRIMARY KEY, sensor TEXT, value REAL, ts INTEGER);
        CREATE TABLE dm_registered (chat_id INTEGER PRIMARY KEY, registered_at INTEGER);
        CREATE TABLE digest_subscriptions (user_id INTEGER, sensor TEXT);
        CREATE TABLE mutes (chat_id INTEGER, sensor TEXT, until_ts INTEGER);
        CREATE TABLE silenced (sensor TEXT PRIMARY KEY, silenced_at INTEGER);
    """)
    now = int(time.time())
    # `ago()` reads the module-level NOW, stamped at import: pin it, or the age
    # printed drifts by however long the suite has been running
    monkeypatch.setattr(sd, "NOW", now)
    con.execute("INSERT INTO dm_registered VALUES (7, ?)", (now - 900,))
    con.execute("INSERT INTO digest_subscriptions VALUES (7, 'CDZ1_I')")
    con.execute("INSERT INTO mutes VALUES (7, 'CDZ1_T', ?)", (now + 3600,))
    con.commit()
    con.close()

    class _C(_Cfg):
        def admins_of(self, name):
            return {7}

        def viewers_of_blackout(self, gid):
            return {7}

    ro = sd.ro_conn(str(dbfile))
    sd.sec_recipients(_C(), ro, [("CDZ1_I", "sensor", "G")])
    ro.close()
    out = capsys.readouterr().out
    assert "15m" in out                    # registration age, not just the id
    assert "CDZ1_T" in out                 # the mute is listed
    assert "never OFFLINE" in out          # and scoped correctly
