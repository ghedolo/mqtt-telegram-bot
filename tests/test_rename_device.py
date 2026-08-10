"""Tests for rename_device.py — the DB half of a device rename.

The script is run by hand, rarely, and its failure mode is silent: any table it
forgets keeps rows under the old sensor name, which no longer match anything.
These pin the table list against the real schema so a table added to bot/db.py
cannot quietly fall out of the rename.
"""
import sqlite3

import pytest

import rename_device
from bot import db as db_module


@pytest.fixture
def renamed_db(tmp_path, monkeypatch):
    """A DB with the real schema and one row per sensor-keyed table for SM1_T."""
    dbfile = tmp_path / "sensors.db"
    monkeypatch.setattr(db_module, "DB_PATH", str(dbfile))
    db_module.init()
    db_module.insert_reading("SM1_T", 21.0)
    db_module.set_threshold("SM1_T", 30.0)
    db_module.subscribe_digest(7, "SM1_T")
    db_module.mute_sensor(7, "SM1_T", until_ts=2_000_000_000)
    db_module.silence_sensor("SM1")          # device-level offline ack
    db_module.insert_alarm("SM1_T", "ALARM", "SM1_T: hot")
    return str(dbfile)


def test_mute_follows_the_rename(renamed_db, monkeypatch):
    # a mute left under the old name stops matching, so the user's alarm DMs
    # resume without them asking — the one consequence a user would feel
    monkeypatch.setattr(db_module, "DB_PATH", renamed_db)
    mapping = rename_device.build_mapping("SM1", "SM9", ["T"])
    rename_device.update_db(renamed_db, mapping, dry_run=False)

    assert db_module.is_muted(7, "SM9_T") is True
    assert db_module.is_muted(7, "SM1_T") is False


def test_every_sensor_keyed_table_is_covered(renamed_db):
    # the schema is the source of truth: any table with a `sensor` column must
    # be in SENSOR_TABLES, or a rename leaves orphans behind in it
    con = sqlite3.connect(renamed_db)
    con.row_factory = sqlite3.Row
    tables = [
        r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    ]
    with_sensor = {
        t for t in tables
        if any(c["name"] == "sensor" for c in con.execute(f"PRAGMA table_info({t})"))
    }
    con.close()
    assert with_sensor <= set(rename_device.SENSOR_TABLES)


def test_dry_run_changes_nothing(renamed_db, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", renamed_db)
    mapping = rename_device.build_mapping("SM1", "SM9", ["T"])
    rename_device.update_db(renamed_db, mapping, dry_run=True)
    assert db_module.get_threshold("SM1_T") == 30.0
    assert db_module.get_threshold("SM9_T") is None
