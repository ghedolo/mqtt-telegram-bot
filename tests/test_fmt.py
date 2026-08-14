"""Tests for bot.fmt — the duration formatter blackout messages report with."""
import pytest

from bot.fmt import fmt_duration


@pytest.mark.parametrize("secs,expected", [
    (0, "0s"),
    (1, "1s"),
    (59, "59s"),
    (60, "1m"),
    (90, "1m 30s"),
    (600, "10m"),
    (3599, "59m 59s"),
    (3600, "1h"),
    (5000, "1h 23m"),
    (86399, "23h 59m"),
    (86400, "1d"),
    (200000, "2d 7h"),
])
def test_fmt_duration(secs, expected):
    assert fmt_duration(secs) == expected


def test_fmt_duration_clamps_negative_and_floats():
    assert fmt_duration(-5) == "0s"
    assert fmt_duration(90.9) == "1m 30s"
