"""Tests for bot.mqtt_client._on_message — payload parsing and robustness to
malformed / oversized / unknown-topic input.

The client dispatches parsed readings via asyncio.run_coroutine_threadsafe;
we stub that to run the (await-free) recorder coroutine synchronously and
capture the (sensor, value) pairs.
"""
from bot import mqtt_client as mqtt_mod
from bot import config


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
  A:
    topic: "t/plain"
    viewers: [ops]
    fields:
      T: {}
  B:
    topic: "t/json"
    viewers: [ops]
    fields:
      Temp:
        json_path: "sensor.temp"
  Z:
    topic: "t/z"
    viewers: [ops]
    fields:
      OCC:
        json_path: occupancy
        states: {false: Assente, true: Presente}
      LUX:
        json_path: illumination
        states: {0: dim, 1: bright}
  AV:
    topic: "t/av"
    availability: true
    viewers: [ops]
    fields:
      T: {}
"""



class Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


class Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, name, value):
        self.calls.append((name, value))


def _build_client(tmp_path, monkeypatch, recorder,
                  on_topic_message=None, on_availability=None):
    sd = tmp_path / "sensors.d"
    sd.mkdir()
    (sd / "00-defaults.yaml").write_text(DEFAULTS)
    cf = tmp_path / "credentials.yaml"
    cf.write_text(CREDS)
    cfg = config.load(str(sd), str(cf))

    mc = mqtt_mod.MqttClient(cfg, recorder,
                             on_topic_message=on_topic_message,
                             on_availability=on_availability)
    mc._loop = object()  # truthy so dispatch fires

    def fake_run(coro, loop):
        # recorder has no awaits -> runs to completion on first send()
        try:
            coro.send(None)
        except StopIteration:
            pass

        class _F:
            def add_done_callback(self, cb):
                pass
        return _F()

    monkeypatch.setattr(mqtt_mod.asyncio, "run_coroutine_threadsafe", fake_run)
    return mc


def test_plain_float(tmp_path, monkeypatch):
    rec = Recorder()
    mc = _build_client(tmp_path, monkeypatch, rec)
    mc._on_message(None, None, Msg("t/plain", b"21.5"))
    assert rec.calls == [("A_T", 21.5)]


def test_json_path_extraction(tmp_path, monkeypatch):
    rec = Recorder()
    mc = _build_client(tmp_path, monkeypatch, rec)
    mc._on_message(None, None, Msg("t/json", b'{"sensor": {"temp": 19.2}}'))
    assert rec.calls == [("B_Temp", 19.2)]


def test_unknown_topic_ignored(tmp_path, monkeypatch):
    rec = Recorder()
    mc = _build_client(tmp_path, monkeypatch, rec)
    mc._on_message(None, None, Msg("t/nope", b"1.0"))
    assert rec.calls == []


def test_non_numeric_plain_dropped(tmp_path, monkeypatch):
    rec = Recorder()
    mc = _build_client(tmp_path, monkeypatch, rec)
    mc._on_message(None, None, Msg("t/plain", b"not-a-number"))
    assert rec.calls == []


def test_malformed_json_dropped(tmp_path, monkeypatch):
    rec = Recorder()
    mc = _build_client(tmp_path, monkeypatch, rec)
    mc._on_message(None, None, Msg("t/json", b"not json"))
    assert rec.calls == []


def test_json_missing_field_skipped(tmp_path, monkeypatch):
    rec = Recorder()
    mc = _build_client(tmp_path, monkeypatch, rec)
    # path sensor.temp absent -> KeyError -> silently skipped (intermittent field)
    mc._on_message(None, None, Msg("t/json", b'{"sensor": {}}'))
    assert rec.calls == []


_Z_PAYLOAD = b'{"illumination": "dim", "linkquality": 255, "occupancy": false}'


def test_bool_and_string_payload_via_states(tmp_path, monkeypatch):
    # occupancy=false -> float(False)=0.0; illumination="dim" -> mapped via
    # the states map used in reverse (label "dim" -> value 0.0).
    rec = Recorder()
    mc = _build_client(tmp_path, monkeypatch, rec)
    mc._on_message(None, None, Msg("t/z", _Z_PAYLOAD))
    assert dict(rec.calls) == {"Z_OCC": 0.0, "Z_LUX": 0.0}


def test_string_state_bright_maps_to_one(tmp_path, monkeypatch):
    rec = Recorder()
    mc = _build_client(tmp_path, monkeypatch, rec)
    mc._on_message(None, None, Msg("t/z", b'{"illumination": "bright", "occupancy": true}'))
    assert dict(rec.calls) == {"Z_OCC": 1.0, "Z_LUX": 1.0}


def test_unknown_state_string_dropped(tmp_path, monkeypatch):
    # a string not among the states labels can't be coerced -> dropped
    rec = Recorder()
    mc = _build_client(tmp_path, monkeypatch, rec)
    mc._on_message(None, None, Msg("t/z", b'{"illumination": "foggy", "occupancy": false}'))
    assert dict(rec.calls) == {"Z_OCC": 0.0}   # LUX dropped, OCC still parsed


def test_oversized_payload_dropped(tmp_path, monkeypatch):
    rec = Recorder()
    mc = _build_client(tmp_path, monkeypatch, rec)
    big = b"0" * (64 * 1024 + 1)
    mc._on_message(None, None, Msg("t/plain", big))
    assert rec.calls == []


# --- availability parsing & dispatch ---

class AvailRecorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, device_key, online):
        self.calls.append((device_key, online))


class TopicRecorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, topic):
        self.calls.append(topic)


def test_parse_availability_formats():
    p = mqtt_mod._parse_availability
    assert p(b'{"state":"online"}') is True
    assert p(b'{"state":"offline"}') is False
    assert p(b"online") is True
    assert p(b"offline") is False
    assert p(b" OFFLINE ") is False          # whitespace + case tolerant
    assert p(b'{"state":"weird"}') is None
    assert p(b"maybe") is None
    assert p(b"not json{") is None


def test_availability_message_dispatched(tmp_path, monkeypatch):
    av = AvailRecorder()
    mc = _build_client(tmp_path, monkeypatch, Recorder(), on_availability=av)
    mc._on_message(None, None, Msg("t/av/availability", b'{"state":"offline"}'))
    mc._on_message(None, None, Msg("t/av/availability", b"online"))
    assert av.calls == [("AV", False), ("AV", True)]


def test_availability_unknown_payload_no_call(tmp_path, monkeypatch):
    av = AvailRecorder()
    mc = _build_client(tmp_path, monkeypatch, Recorder(), on_availability=av)
    mc._on_message(None, None, Msg("t/av/availability", b"garbage"))
    assert av.calls == []


def test_availability_topic_not_routed_as_reading_or_topic_message(tmp_path, monkeypatch):
    rec = Recorder()
    tr = TopicRecorder()
    av = AvailRecorder()
    mc = _build_client(tmp_path, monkeypatch, rec,
                       on_topic_message=tr, on_availability=av)
    mc._on_message(None, None, Msg("t/av/availability", b"offline"))
    assert rec.calls == []      # not parsed as a reading
    assert tr.calls == []       # must not feed the data-cadence heuristic
    assert av.calls == [("AV", False)]
