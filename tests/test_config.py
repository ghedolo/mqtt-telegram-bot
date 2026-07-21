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
        admins: []
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


def test_resolve_device_is_case_insensitive_and_canonical(tmp_path):
    cfg = _write_env(tmp_path)
    assert cfg.resolve_device("SM1") == "SM1"      # exact
    assert cfg.resolve_device("sm1") == "SM1"      # lowercased
    assert cfg.resolve_device("Sm1") == "SM1"      # mixed
    assert cfg.resolve_device("nope") == "nope"    # unknown passes through


def test_resolve_sensor_is_case_insensitive(tmp_path):
    cfg = _write_env(tmp_path)
    assert cfg.resolve_sensor("sm1_t") == "SM1_T"
    assert cfg.resolve_sensor("SM1_T") == "SM1_T"


def test_defaults_and_new_keys(tmp_path):
    cfg = _write_env(tmp_path)
    assert cfg.retention_days == 30
    assert cfg.archive_time == "12:00"   # default when absent
    assert cfg.enable_menu is True       # default when absent
    assert cfg.digest_time == "15:00"
    assert cfg.trace_cmd is False        # command trace off unless asked
    assert cfg.trace_cmd_file == "cmdtrace.log"


def test_trace_cmd_opts_parse(tmp_path):
    creds = CREDS.replace(
        '  group_id: -100',
        '  group_id: -100\n  traceCmd: 1\n  traceCmdFile: "/var/log/lorte/cmd.log"',
    )
    cfg = _write_env(tmp_path, creds=creds)
    assert cfg.trace_cmd is True
    assert cfg.trace_cmd_file == "/var/log/lorte/cmd.log"


def test_field_viewers_override_replaces_device(tmp_path):
    cfg = _write_env(tmp_path)
    # SM1_I declares its own viewers -> replaces device-level [ops]
    assert cfg.sensors["SM1_I"].viewers == ["nobody"]


CREDS_OPS2 = CREDS.replace("  ops: [1, 2]", "  ops: [1, 2]\n  ops2: [4]")


def _with_field_H(body: str) -> str:
    return DEFAULTS + "      H:\n" + body


def test_field_declaring_only_admins_warns(tmp_path):
    # The override replaces both keys, so `admins:` alone leaves the field with
    # no viewers. Load succeeds — a monitoring bot must not refuse to start over
    # an access nit — but says so, and the blanking is real.
    cfg = _write_env(tmp_path, defaults=_with_field_H("        admins: [ops2]\n"),
                     creds=CREDS_OPS2)
    assert len(cfg.warnings) == 1
    assert "SM1.H" in cfg.warnings[0]
    assert "declares 'admins' but not 'viewers'" in cfg.warnings[0]
    assert cfg.sensors["SM1_H"].viewers == []      # the warning is not cosmetic


def test_field_declaring_only_viewers_warns(tmp_path):
    cfg = _write_env(tmp_path, defaults=_with_field_H("        viewers: [ops2]\n"),
                     creds=CREDS_OPS2)
    assert len(cfg.warnings) == 1
    assert "declares 'viewers' but not 'admins'" in cfg.warnings[0]
    assert cfg.sensors["SM1_H"].admins == []


def test_clean_config_has_no_warnings(tmp_path):
    assert _write_env(tmp_path).warnings == []


def test_field_stating_both_replaces_device_lists(tmp_path):
    cfg = _write_env(
        tmp_path,
        defaults=_with_field_H("        viewers: []\n        admins: [ops2]\n"),
        creds=CREDS_OPS2,
    )
    h = cfg.sensors["SM1_H"]
    assert h.admins == ["ops2"]
    assert h.viewers == []                       # explicit: nobody beyond the admins
    assert cfg.viewers_of("SM1_H") == {4}        # admin implies viewer, and only 4
    assert cfg.is_viewer(1, "SM1_H") is False    # ops lost the field entirely
    assert cfg.is_viewer(1, "SM1_T") is True     # sibling field unaffected


def test_field_with_neither_key_inherits_both(tmp_path):
    cfg = _write_env(tmp_path, defaults=_with_field_H("        unit: '%'\n"))
    h = cfg.sensors["SM1_H"]
    assert h.viewers == ["ops"] and h.admins == []


def test_empty_access_lists_parse_as_empty_not_missing(tmp_path):
    # `viewers:` with nothing after it is None in YAML, not [] — both spellings
    # must mean "no groups", and neither may count as an absent key.
    cfg = _write_env(tmp_path,
                     defaults=_with_field_H("        viewers:\n        admins: []\n"))
    h = cfg.sensors["SM1_H"]
    assert h.viewers == [] and h.admins == []
    assert cfg.viewers_of("SM1_H") == set()      # visible to nobody, fail-closed


def test_device_empty_access_lists_parse_as_empty_not_missing(tmp_path):
    # Same rule one level up: a bare `viewers:` on the device must read as "no
    # groups" and be inherited as such, not blow up on list(None).
    cfg = _write_env(tmp_path, defaults=DEFAULTS.replace("    viewers: [ops]\n",
                                                         "    viewers:\n    admins:\n"))
    t = cfg.sensors["SM1_T"]                     # inherits both from the device
    assert t.viewers == [] and t.admins == []
    assert cfg.viewers_of("SM1_T") == set()      # visible to nobody, fail-closed
    assert cfg.warnings == []                    # device level raises no warning


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


# --- state render tables (discrete fields, e.g. a door contact) ---

_STATES_BASE = """
devices:
  DK1:
    topic: "t/dk1"
    viewers: [ops]
    fields:
      contact:
        json_path: contact
        decimals: 1
        states:
{keys}
"""


def _states_cfg(tmp_path, keys):
    defaults = _STATES_BASE.format(keys=keys)
    return _write_env(tmp_path, defaults=defaults)


@pytest.mark.parametrize("keys", [
    "          false: Aperta\n          true: Chiusa",   # bool keys
    "          0: Aperta\n          1: Chiusa",           # int keys
    '          "0": Aperta\n          "1": Chiusa',       # string keys
])
def test_states_key_forms_all_normalise_to_float(tmp_path, keys):
    cfg = _states_cfg(tmp_path, keys)
    sc = cfg.sensors["DK1_contact"]
    assert sc.states == {0.0: "Aperta", 1.0: "Chiusa"}


def test_fmt_renders_state_label(tmp_path):
    cfg = _states_cfg(tmp_path, "          false: Aperta\n          true: Chiusa")
    assert cfg.fmt("DK1_contact", 0.0) == "Aperta"
    assert cfg.fmt("DK1_contact", 1.0) == "Chiusa"


def test_fmt_falls_back_to_number_for_unmapped_value(tmp_path):
    # A threshold like 0.5 is not in the table -> render numerically, so an
    # alarm message never claims "0 < thr_low 0".
    cfg = _states_cfg(tmp_path, "          false: Aperta\n          true: Chiusa")
    assert cfg.fmt("DK1_contact", 0.5) == "0.5"


def test_fmt_without_states_is_plain_number(tmp_path):
    cfg = _write_env(tmp_path)          # SM1_T has no states
    assert cfg.fmt("SM1_T", 21.5) == "21.5"


def test_states_non_numeric_key_rejected(tmp_path):
    bad = "          open: Aperta"     # 'open' is not numeric
    with pytest.raises(ValueError, match="'states' must map"):
        _states_cfg(tmp_path, bad)


# --- signals (non-stored fields, consumed only for blackout detection) ---

_SIGNAL_BASE = """
defaults:
  interval: 300
devices:
  SM1:
    topic: "t/sm1"
    viewers: [ops]
    fields:
      I:
        unit: A
      IF:
        signal: true
        topic: "t/sm1fast"
        json_path: cur
blackouts:
  SIG:
    fields: [SM1_IF]
    below: 0.5
    for_seconds: 0
    stale_after: 9
"""


def test_signal_lands_in_signals_not_sensors(tmp_path):
    cfg = _write_env(tmp_path, defaults=_SIGNAL_BASE)
    assert "SM1_IF" in cfg.signals
    assert "SM1_IF" not in cfg.sensors          # excluded from the flat sensor view
    assert "SM1_I" in cfg.sensors
    sig = cfg.signals["SM1_IF"]
    assert sig.topic == "t/sm1fast"
    assert sig.json_path == "cur"


def test_signal_excluded_from_visible_sensors(tmp_path):
    cfg = _write_env(tmp_path, defaults=_SIGNAL_BASE)
    assert "SM1_IF" not in cfg.visible_sensors(1)
    assert cfg.is_signal("SM1_IF") is True
    assert cfg.is_signal("SM1_I") is False


def test_signal_not_in_device_fields(tmp_path):
    # kept out of device.fields so the per-device offline check ignores it
    cfg = _write_env(tmp_path, defaults=_SIGNAL_BASE)
    assert set(cfg.devices["SM1"].fields) == {"I"}


def test_blackout_accepts_signal_field_and_resolves_viewers(tmp_path):
    cfg = _write_env(tmp_path, defaults=_SIGNAL_BASE)
    assert cfg.blackouts["SIG"].fields == ["SM1_IF"]
    # signal inherits device viewers -> ops members can view/subscribe the group
    assert cfg.viewers_of("SM1_IF") == {1, 2}
    assert cfg.is_viewer_of_blackout(1, "SIG") is True


def test_signal_name_collision_with_sensor_rejected(tmp_path):
    # A signal must share the derived-name namespace with sensors. Device "A"
    # field "B_C" and device "A_B" field "C" both derive "A_B_C"; the signal
    # collides with the sensor and is rejected.
    bad = """
devices:
  A:
    topic: "t/a"
    fields:
      B_C: {}
  A_B:
    topic: "t/b"
    fields:
      C: {signal: true, topic: "t/c"}
"""
    with pytest.raises(ValueError, match="Duplicate sensor name"):
        _write_env(tmp_path, defaults=bad)


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


def test_blackout_stale_after_must_be_positive(tmp_path):
    bad = BLACKOUT_BASE.replace("stale_after: 15", "stale_after: 0")
    with pytest.raises(ValueError, match="'stale_after' must be > 0"):
        _write_env(tmp_path, defaults=bad)


def test_blackout_for_seconds_negative_rejected(tmp_path):
    bad = BLACKOUT_BASE.replace("for_seconds: 10", "for_seconds: -1")
    with pytest.raises(ValueError, match="'for_seconds' must be >= 0"):
        _write_env(tmp_path, defaults=bad)


def test_blackout_viewers_resolved_from_watched_fields(tmp_path):
    cfg = _write_env(tmp_path, defaults=BLACKOUT_BASE)
    # SM1 device viewers = [ops] -> ops members can view the R2 blackout
    assert cfg.is_viewer_of_blackout(1, "R2") is True
    assert cfg.is_viewer_of_blackout(999, "R2") is False


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
