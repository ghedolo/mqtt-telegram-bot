"""Tests for bot.db — storage, history window, and the archive cutoff.

The archive tests pin down the regression that left readings_archive empty:
archive_old_readings must move rows strictly older than
now - retention_days*86400 and keep the rest.
"""
import time


def test_insert_and_get_latest(temp_db):
    temp_db.insert_reading("A_T", 21.5, ts=1000)
    temp_db.insert_reading("A_T", 22.0, ts=2000)
    row = temp_db.get_latest("A_T")
    assert row["value"] == 22.0
    assert row["ts"] == 2000


def test_get_latest_missing_sensor(temp_db):
    assert temp_db.get_latest("nope") is None


def test_get_history_window_and_order(temp_db):
    now = int(time.time())
    temp_db.insert_reading("A_T", 1.0, ts=now - 10 * 3600)  # outside 8h
    temp_db.insert_reading("A_T", 3.0, ts=now - 60)          # inside
    temp_db.insert_reading("A_T", 2.0, ts=now - 3600)        # inside
    rows = temp_db.get_history("A_T", seconds=8 * 3600)
    # only in-window rows, ascending by ts
    assert [r["value"] for r in rows] == [2.0, 3.0]


def test_archive_moves_old_keeps_recent(temp_db):
    now = int(time.time())
    old_ts = now - 40 * 86400
    new_ts = now - 1 * 86400
    temp_db.insert_reading("A_T", 10.0, ts=old_ts)
    temp_db.insert_reading("A_T", 11.0, ts=new_ts)

    temp_db.archive_old_readings(30)

    stats = temp_db.get_db_stats()
    assert stats["readings"]["count"] == 1
    assert stats["archive"]["count"] == 1
    # the surviving reading is the recent one
    remaining = temp_db.get_history("A_T", seconds=60 * 86400)
    assert [r["ts"] for r in remaining] == [new_ts]


def test_archive_noop_when_all_recent(temp_db):
    now = int(time.time())
    temp_db.insert_reading("A_T", 1.0, ts=now - 5 * 86400)
    temp_db.archive_old_readings(30)
    stats = temp_db.get_db_stats()
    assert stats["archive"]["count"] == 0
    assert stats["readings"]["count"] == 1


def test_archive_boundary_is_strict(temp_db):
    # a row exactly at the cutoff must NOT be archived (WHERE ts < cutoff)
    now = int(time.time())
    cutoff = now - 30 * 86400
    temp_db.insert_reading("A_T", 1.0, ts=cutoff)        # exactly at cutoff -> kept
    temp_db.insert_reading("A_T", 2.0, ts=cutoff - 1)    # just older -> archived
    temp_db.archive_old_readings(30)
    stats = temp_db.get_db_stats()
    assert stats["readings"]["count"] == 1
    assert stats["archive"]["count"] == 1


def test_thresholds_set_and_partial_clear(temp_db):
    temp_db.set_threshold("A_T", 30.0)
    temp_db.set_threshold_low("A_T", 10.0)
    assert temp_db.get_threshold("A_T") == 30.0
    assert temp_db.get_threshold_low("A_T") == 10.0

    # clearing only the high threshold keeps the row (low still set)
    temp_db.clear_threshold("A_T")
    assert temp_db.get_threshold("A_T") is None
    assert temp_db.get_threshold_low("A_T") == 10.0

    # clearing the last remaining threshold drops the row entirely
    temp_db.clear_threshold_low("A_T")
    assert temp_db.get_threshold_low("A_T") is None
    assert temp_db.get_all_thresholds() == {}
    assert temp_db.get_all_thresholds_low() == {}


def test_mute_expiry(temp_db):
    now = int(time.time())
    temp_db.mute_sensor(1, "A_T", until_ts=now + 3600)
    assert temp_db.is_muted(1, "A_T") is True
    # re-muting into the past expires it; is_muted purges stale rows
    temp_db.mute_sensor(1, "A_T", until_ts=now - 1)
    assert temp_db.is_muted(1, "A_T") is False
    assert temp_db.get_active_mutes(1) == []


def test_forget_device_archives_and_clears(temp_db):
    temp_db.insert_reading("A_T", 5.0, ts=1000)
    temp_db.set_threshold("A_T", 9.0)
    temp_db.forget_device(["A_T"], "A")

    assert temp_db.get_history("A_T", seconds=10 ** 9) == []
    assert temp_db.get_threshold("A_T") is None
    stats = temp_db.get_db_stats()
    assert stats["archive"]["count"] == 1


def test_digest_subscriptions_roundtrip(temp_db):
    temp_db.subscribe_digest(7, "A_T")
    temp_db.subscribe_digest(7, "B_H")
    temp_db.subscribe_digest(7, "A_T")  # idempotent
    assert temp_db.get_digest_subscriptions(7) == ["A_T", "B_H"]
    temp_db.unsubscribe_digest(7, "A_T")
    assert temp_db.get_digest_subscriptions(7) == ["B_H"]
