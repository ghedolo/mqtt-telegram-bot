"""Tests for bot.config.load — YAML parsing, defaults, and validation errors.

Each test writes a throwaway sensors.d/ + credentials.yaml under tmp_path and
loads it, so no real config is touched.
"""
import pytest

from bot import config


CREDS = """
telegram:
  token: "T"
  group_id: -100
mqtt:
  host: "broker"
  port: 1883
groups:
  ops: [1, 2]
superadmin: [9]
"""

DEFAULTS = """
defaults:
  interval: 300
  retention_days: 30
devices:
  SM1:
    topic: "t/sm1"
    info: "Sala"
    viewers: [ops]
    fields:
      T:
        unit: "°C"
      I:
        decimals: 2
        viewers: [nobody]
"""


def _write_env(tmp_path, defaults=DEFAULTS, extra=None, creds=CREDS):
    sd = tmp_path / "sensors.d"
    sd.mkdir()
    (sd / "00-defaults.yaml").write_text(defaults)
    for name, content in (extra or {}).items():
        (sd / name).write_text(content)
    cf = tmp_path / "credentials.yaml"
    cf.write_text(creds)
    return config.load(str(sd), str(cf))


def test_basic_parse_and_derived_names(tmp_path):
    cfg = _write_env(tmp_path)
    assert set(cfg.sensors) == {"SM1_T", "SM1_I"}
    t = cfg.sensors["SM1_T"]
    assert t.unit == "°C"
    assert t.decimals == 1               # default
    assert t.interval == 300             # inherited from defaults
    assert t.viewers == ["ops"]          # inherited from device
    assert cfg.sensors["SM1_I"].decimals == 2


def test_defaults_and_new_keys(tmp_path):
    cfg = _write_env(tmp_path)
    assert cfg.retention_days == 30
    assert cfg.archive_time == "12:00"   # default when absent
    assert cfg.enable_menu is True       # default when absent
    assert cfg.digest_time == "15:00"


def test_field_viewers_override_replaces_device(tmp_path):
    cfg = _write_env(tmp_path)
    # SM1_I declares its own viewers -> replaces device-level [ops]
    assert cfg.sensors["SM1_I"].viewers == ["nobody"]


def test_access_helpers(tmp_path):
    cfg = _write_env(tmp_path)
    assert cfg.is_viewer(1, "SM1_T") is True
    assert cfg.is_viewer(99, "SM1_T") is False
    assert cfg.is_superadmin(9) is True


def test_duplicate_device_key_across_files(tmp_path):
    dup = """
devices:
  SM1:
    topic: "t/other"
    fields:
      X: {}
"""
    with pytest.raises(ValueError, match="Duplicate device key"):
        _write_env(tmp_path, extra={"10-dup.yaml": dup})


def test_case_insensitive_name_collision(tmp_path):
    defaults = """
defaults:
  interval: 300
devices:
  A:
    topic: "t/a"
    fields:
      T: {}
      t: {}
"""
    with pytest.raises(ValueError, match="differ only by case"):
        _write_env(tmp_path, defaults=defaults)


def test_decimals_out_of_range(tmp_path):
    defaults = """
devices:
  A:
    topic: "t/a"
    fields:
      T:
        decimals: 7
"""
    with pytest.raises(ValueError, match="decimals must be 0-5"):
        _write_env(tmp_path, defaults=defaults)


def test_stray_key_in_non_defaults_file(tmp_path):
    # only 00-defaults.yaml may carry a defaults: block
    bad = """
defaults:
  interval: 10
devices:
  Z:
    topic: "t/z"
    fields:
      T: {}
"""
    with pytest.raises(ValueError, match="Unexpected top-level key"):
        _write_env(tmp_path, extra={"20-bad.yaml": bad})


# --- blackout validation ---

BLACKOUT_BASE = """
defaults:
  interval: 300
devices:
  SM1:
    topic: "t/sm1"
    viewers: [ops]
    fields:
      I: {}
blackouts:
  R2:
    fields: [SM1_I]
    below: 0.5
    for_seconds: 10
    stale_after: 15
"""


def test_blackout_valid(tmp_path):
    cfg = _write_env(tmp_path, defaults=BLACKOUT_BASE)
    assert "R2" in cfg.blackouts
    g = cfg.blackouts["R2"]
    assert g.fields == ["SM1_I"]
    assert g.below == 0.5


def test_blackout_unknown_field(tmp_path):
    bad = BLACKOUT_BASE.replace("fields: [SM1_I]", "fields: [SM1_NOPE]")
    with pytest.raises(ValueError, match="unknown field"):
        _write_env(tmp_path, defaults=bad)


def test_blackout_below_must_be_positive(tmp_path):
    bad = BLACKOUT_BASE.replace("below: 0.5", "below: 0")
    with pytest.raises(ValueError, match="'below' must be > 0"):
        _write_env(tmp_path, defaults=bad)


def test_blackout_collides_with_sensor_name(tmp_path):
    bad = BLACKOUT_BASE.replace("  R2:", "  SM1_I:")
    with pytest.raises(ValueError, match="collides with a sensor name"):
        _write_env(tmp_path, defaults=bad)


# --- topics & credentials ---

def test_duplicate_topic_rejected(tmp_path):
    defaults = """
devices:
  A:
    topic: "t/same"
    fields:
      T: {}
  B:
    topic: "t/same"
    fields:
      T: {}
"""
    with pytest.raises(ValueError, match="Duplicate topic"):
        _write_env(tmp_path, defaults=defaults)


def test_field_without_topic_rejected(tmp_path):
    defaults = """
devices:
  A:
    fields:
      T: {}
"""
    with pytest.raises(ValueError, match="has no topic"):
        _write_env(tmp_path, defaults=defaults)


def test_mqtt_tls_inferred_from_port_8883(tmp_path):
    creds = CREDS.replace("port: 1883", "port: 8883")
    cfg = _write_env(tmp_path, creds=creds)
    assert cfg.mqtt_tls is True


def test_mqtt_tls_off_on_plain_port(tmp_path):
    cfg = _write_env(tmp_path)  # port 1883
    assert cfg.mqtt_tls is False


def test_poll_interval_clamped(tmp_path):
    creds = CREDS.replace("group_id: -100", "group_id: -100\n  poll_interval: 99")
    cfg = _write_env(tmp_path, creds=creds)
    assert cfg.poll_interval == 10   # clamped to max


def test_group_ids_coerced_to_int(tmp_path):
    creds = """
telegram:
  token: "T"
  group_id: "-100"
mqtt:
  host: "broker"
  port: 1883
groups:
  ops: ["1", "2"]
superadmin: ["9"]
"""
    cfg = _write_env(tmp_path, creds=creds)
    assert cfg.groups["ops"] == [1, 2]
    assert cfg.superadmin == [9]
    assert cfg.telegram_group_id == -100
