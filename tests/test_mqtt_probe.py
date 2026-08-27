"""Tests for mqtt_probe.py — showing what the bot would parse from a raw payload.

The tool exists for one silent branch: `MqttClient._on_message` drops a field
whose payload parses but does not contain it (`KeyError`/`TypeError` →
`continue`, no log). A meter then publishes on the broker while the bot stores
nothing and raises no OFFLINE, because the topic itself was received. These pin
that the explanation matches what the dispatch loop really does.
"""
import mqtt_probe as mp


class _Sc:
    def __init__(self, name, topic, json_path=None, states=None):
        self.name, self.topic, self.json_path, self.states = name, topic, json_path, states


def test_a_present_field_reports_the_value_the_bot_would_store():
    sc = _Sc("SM1_CDZ1_I", "t", json_path="I")
    assert mp.explain(sc, '{"I": 4.2}') == "value 4.2"


def test_a_missing_field_names_the_silent_branch_and_the_keys_that_are_there():
    # the whole point: this is the case that produces no reading, no log and no
    # OFFLINE, so the report has to name it and show what the payload does carry
    sc = _Sc("SM1_CDZ1_I", "t", json_path="I")
    out = mp.explain(sc, '{"current": 4.2, "V": 230}')
    assert "SKIPPED SILENTLY" in out
    assert "'I'" in out and "current" in out and "V" in out


def test_a_nested_path_walks_every_segment():
    sc = _Sc("X", "t", json_path="data.phase1.I")
    assert mp.explain(sc, '{"data": {"phase1": {"I": 1.5}}}') == "value 1.5"
    assert "SKIPPED SILENTLY" in mp.explain(sc, '{"data": {"phase2": {"I": 1.5}}}')


def test_a_json_path_against_a_bare_payload_is_reported_not_crashed():
    # `json.loads("4.2")` succeeds and yields a float, so the walk would raise
    # TypeError and be swallowed: the report must say why, not crash or lie
    sc = _Sc("X", "t", json_path="I")
    out = mp.explain(sc, "4.2")
    assert "SKIPPED SILENTLY" in out and "bare float" in out
    assert "not JSON" in mp.explain(sc, "hello there")


def test_a_plain_numeric_payload_needs_no_json_path():
    assert mp.explain(_Sc("X", "t"), "4.2") == "value 4.2"


def test_a_discrete_payload_resolves_through_the_states_map():
    # same reverse label→value lookup the dispatch loop uses
    sc = _Sc("DOOR", "t", states={0.0: "Chiusa", 1.0: "Aperta"})
    assert mp.explain(sc, "Aperta") == "value 1.0"
    assert "matches no `states` label" in mp.explain(sc, "socchiusa")


class _Cfg:
    class _F:
        def __init__(self, name, topic):
            self.name, self.topic, self.json_path, self.states = name, topic, None, None

    class _Dev:
        def __init__(self, fields):
            self.fields = fields

    def __init__(self):
        self.sensors = {"C1_I": self._F("C1_I", "t/i1"), "C1_T": self._F("C1_T", "t/t1")}
        self.signals = {"C1_IF": self._F("C1_IF", "t/if1")}
        grp = type("G", (), {"fields": ["C1_IF"]})()
        self.blackouts = {"G": grp}
        self.devices = {"C1": self._Dev({"I": self.sensors["C1_I"],
                                         "T": self.sensors["C1_T"],
                                         "IF": self.signals["C1_IF"]})}

    def device_of(self, name):
        return "C1"


def test_default_scope_covers_the_blackout_fields_and_their_device():
    # a group watching only a Signal must still bring in the device's stored
    # fields, or the probe listens to one topic and explains nothing
    topics = mp.pick(_Cfg(), [], False)
    assert set(topics) == {"t/if1", "t/i1", "t/t1"}


def test_patterns_and_all_select_explicitly():
    assert set(mp.pick(_Cfg(), ["C1_I"], False)) == {"t/i1"}
    assert set(mp.pick(_Cfg(), [], True)) == {"t/i1", "t/t1", "t/if1"}
