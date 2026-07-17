"""Tests for bot.graph.

prepare_series (pure data prep) is unit-tested directly for glitch filtering
and gap breaks; build() is smoke-tested to confirm it renders a PNG without
crashing on normal, glitchy, and empty inputs.
"""
import math

from bot import graph


def _rows(pairs):
    return [{"ts": ts, "value": v} for ts, v in pairs]


def test_prepare_series_plain():
    s = graph.prepare_series(_rows([(1000, 20.0), (1010, 21.0)]),
                             gap_thr=100, vmin_b=None, vmax_b=None)
    assert s.line_vals == [20.0, 21.0]
    assert [v for _, v in s.in_vals] == [20.0, 21.0]
    assert s.hi_times == [] and s.lo_times == []


def test_prepare_series_high_glitch_dropped():
    s = graph.prepare_series(_rows([(1000, 20.0), (1010, 999.0), (1020, 21.0)]),
                             gap_thr=100, vmin_b=-50, vmax_b=100)
    # the 999.0 reading is out of range: NaN in the line, not in in_vals
    assert len(s.hi_times) == 1
    assert [v for _, v in s.in_vals] == [20.0, 21.0]
    assert any(math.isnan(v) for v in s.line_vals)


def test_prepare_series_low_glitch_dropped():
    s = graph.prepare_series(_rows([(1000, -80.0), (1010, 20.0)]),
                             gap_thr=100, vmin_b=-50, vmax_b=100)
    assert len(s.lo_times) == 1
    assert [v for _, v in s.in_vals] == [20.0]


def test_prepare_series_no_bounds_keeps_everything():
    s = graph.prepare_series(_rows([(1000, 999.0), (1010, -999.0)]),
                             gap_thr=100, vmin_b=None, vmax_b=None)
    assert s.hi_times == [] and s.lo_times == []
    assert [v for _, v in s.in_vals] == [999.0, -999.0]


def test_prepare_series_gap_inserts_break():
    # dt 500 > gap_thr 100 -> a NaN breakpoint is inserted between the readings
    s = graph.prepare_series(_rows([(1000, 20.0), (1500, 21.0)]),
                             gap_thr=100, vmin_b=None, vmax_b=None)
    assert sum(1 for v in s.line_vals if math.isnan(v)) == 1
    # two real points + one break point
    assert len(s.times) == 3
    assert [v for _, v in s.in_vals] == [20.0, 21.0]


def test_prepare_series_no_gap_when_within_threshold():
    s = graph.prepare_series(_rows([(1000, 20.0), (1050, 21.0)]),
                             gap_thr=100, vmin_b=None, vmax_b=None)
    assert not any(math.isnan(v) for v in s.line_vals)
    assert len(s.times) == 2


# --- build() smoke tests: renders a PNG, no crash ---

def _is_png(buf):
    data = buf.getvalue()
    return len(data) > 0 and data[:8] == b"\x89PNG\r\n\x1a\n"


def test_build_renders_png(monkeypatch):
    monkeypatch.setattr(graph.db, "get_history",
                        lambda name, seconds: _rows([(1000, 20.0), (1010, 21.5)]))
    buf = graph.build([("A_T", 30.0, "°C", -20, 80, 300, 1, None)], hours=8)
    assert _is_png(buf)


def test_build_handles_no_data(monkeypatch):
    monkeypatch.setattr(graph.db, "get_history", lambda name, seconds: [])
    buf = graph.build([("A_T", None, "°C", None, None, 300, 1, None)], hours=8)
    assert _is_png(buf)


def test_build_multi_sensor_with_glitch(monkeypatch):
    monkeypatch.setattr(graph.db, "get_history",
                        lambda name, seconds: _rows([(1000, 20.0), (1010, 999.0), (1020, 21.0)]))
    buf = graph.build([
        ("A_T", 30.0, "°C", -20, 80, 300, 1, None),
        ("B_H", None, "%", 0, 100, 300, 0, None),
    ], hours=8)
    assert _is_png(buf)


def test_build_state_field_renders_as_steps(monkeypatch):
    # a door contact: discrete states -> steps + labeled y-axis, no crash
    monkeypatch.setattr(graph.db, "get_history",
                        lambda name, seconds: _rows([(1000, 1.0), (1010, 0.0), (1020, 1.0)]))
    states = {0.0: "Aperta", 1.0: "Chiusa"}
    buf = graph.build([("DK1_C", 0.5, "", None, None, 3600, 1, states)], hours=8)
    assert _is_png(buf)
