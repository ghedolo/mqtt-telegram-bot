"""Tests for split_device.py — moving fields out of a device into a new one.

Same reasoning as tests/test_rename_device.py: the script is run by hand, and
its failure modes are quiet. A field left behind in the old block keeps feeding
the old device's offline check (the exact bug the split exists to fix); a sensor
name left stale in a blackout group stops the bot from starting at all.
"""
import sqlite3
import textwrap

import pytest

import split_device
from bot import db as db_module


CONFIG = textwrap.dedent("""\
    devices:
      SM1_UTA1:
        info: SM1/UTA1/GA4
        admins:
        - ops
        viewers:
        - ccib
        fields:
          T:
            topic: GA4/temperature
            unit: °C
            validMax: 80
          H:
            topic: GA4/humidity
            unit: '%'
          I:
            topic: R2Env/cdz/I1   # inline comment must survive the move
            unit: 'A'
            decimals: 2
          IF:
            topic: R2Env/cdz/I1_fast
            unit: 'A'
            signal: true
      SM1_UTA2:
        info: SM1/UTA2
        fields:
          T:
            topic: GA5/temperature
    """)


def _split(text=CONFIG, old="SM1_UTA1", new="SM1_CDZ1", fields=("I", "IF")):
    return split_device.split_yaml_text(text, old, new, list(fields))


def _devices(text):
    import yaml
    return yaml.safe_load(text)["devices"]


def test_moved_fields_leave_the_old_device():
    # the bug being fixed: a 3 s current meter under the same key as a room
    # probe keeps the device's offline check alive while the probe is dead
    devs = _devices(_split())
    assert sorted(devs["SM1_UTA1"]["fields"]) == ["H", "T"]
    assert sorted(devs["SM1_CDZ1"]["fields"]) == ["I", "IF"]


def test_new_device_inherits_device_level_keys():
    devs = _devices(_split())
    assert devs["SM1_CDZ1"]["admins"] == ["ops"]
    assert devs["SM1_CDZ1"]["viewers"] == ["ccib"]
    assert devs["SM1_CDZ1"]["info"] == "SM1/UTA1/GA4"


def test_field_bodies_are_carried_over_intact():
    devs = _devices(_split())
    moved = devs["SM1_CDZ1"]["fields"]
    assert moved["I"] == {"topic": "R2Env/cdz/I1", "unit": "A", "decimals": 2}
    assert moved["IF"]["signal"] is True
    # comments are not visible through yaml.safe_load, so assert on the text
    assert "# inline comment must survive the move" in _split()


def test_other_devices_are_untouched():
    text = _split()
    devs = _devices(text)
    assert devs["SM1_UTA2"] == {"info": "SM1/UTA2", "fields": {"T": {"topic": "GA5/temperature"}}}
    assert "SM1_UTA2" in text.split("SM1_CDZ1:")[1]  # new block inserted before it, not over it


def test_moving_every_field_is_refused():
    # that is a rename; doing it here would leave a device with an empty
    # `fields:` and silently drop it out of every listing
    with pytest.raises(SystemExit):
        _split(fields=("T", "H", "I", "IF"))


def test_unknown_field_is_refused():
    with pytest.raises(SystemExit):
        _split(fields=("I", "NOPE"))


def test_unknown_device_is_refused():
    with pytest.raises(SystemExit):
        _split(old="SM9_UTA9")


def test_mapping_excludes_the_bare_device_key():
    # OLD survives a split, so its device-level rows (OFFLINE alarms, ackOff
    # silence) must stay put — remapping them would hand the probe's outage
    # history to the meter
    mapping = split_device.build_mapping("SM1_UTA1", "SM1_CDZ1", ["I", "IF"])
    assert mapping == {"SM1_UTA1_I": "SM1_CDZ1_I", "SM1_UTA1_IF": "SM1_CDZ1_IF"}


def test_reference_rewrite_is_word_bounded(tmp_path):
    # SM1_UTA1_I is a prefix of SM1_UTA1_IF: a plain replace would turn the
    # latter into SM1_CDZ1_IF via the former and corrupt one of the two
    d = tmp_path / "sensors.d"
    d.mkdir()
    (d / "00-defaults.yaml").write_text(textwrap.dedent("""\
        blackouts:
          r2:
            fields:
            - SM1_UTA1_I
            - SM1_UTA1_IF
            below: 0.5
        """))
    dev_file = d / "sm1.yaml"
    dev_file.write_text(CONFIG)
    mapping = split_device.build_mapping("SM1_UTA1", "SM1_CDZ1", ["I", "IF"])
    split_device.update_references(str(d), str(dev_file), mapping, dry_run=False)

    import yaml
    got = yaml.safe_load((d / "00-defaults.yaml").read_text())
    assert got["blackouts"]["r2"]["fields"] == ["SM1_CDZ1_I", "SM1_CDZ1_IF"]
    # the device file itself is update_yaml's job, and must not be double-edited
    assert dev_file.read_text() == CONFIG


@pytest.fixture
def split_db(tmp_path, monkeypatch):
    """A DB with the real schema and rows under both halves of the old device."""
    dbfile = tmp_path / "sensors.db"
    monkeypatch.setattr(db_module, "DB_PATH", str(dbfile))
    db_module.init()
    db_module.insert_reading("SM1_UTA1_I", 3.2)
    db_module.insert_reading("SM1_UTA1_T", 21.0)
    db_module.set_threshold("SM1_UTA1_I", 15.0)
    db_module.subscribe_digest(7, "SM1_UTA1_I")
    db_module.mute_sensor(7, "SM1_UTA1_I", until_ts=2_000_000_000)
    db_module.insert_alarm("SM1_UTA1_I", "ALARM", "SM1_UTA1_I: high")
    db_module.insert_alarm("SM1_UTA1", "OFFLINE", "OFFLINE SM1_UTA1")
    db_module.silence_sensor("SM1_UTA1")
    return str(dbfile)


def test_db_migration_moves_only_the_split_fields(split_db, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", split_db)
    mapping = split_device.build_mapping("SM1_UTA1", "SM1_CDZ1", ["I", "IF"])
    split_device.update_db(split_db, mapping, dry_run=False)

    assert db_module.get_latest("SM1_CDZ1_I")["value"] == 3.2
    assert db_module.get_latest("SM1_UTA1_I") is None
    assert db_module.get_threshold("SM1_CDZ1_I") == 15.0
    assert db_module.is_muted(7, "SM1_CDZ1_I") is True
    # untouched: the probe's readings and the old device's own offline state
    assert db_module.get_latest("SM1_UTA1_T")["value"] == 21.0
    assert db_module.is_silenced("SM1_UTA1") is True

    con = sqlite3.connect(split_db)
    kinds = [
        r[0] for r in con.execute("SELECT kind FROM alarms WHERE sensor='SM1_UTA1'")
    ]
    con.close()
    assert kinds == ["OFFLINE"]
