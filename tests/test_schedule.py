"""Tests for bot.schedule — the fixed-time scheduling that replaced the
relative sleep(86400) archive timer.
"""
from datetime import datetime

from bot.schedule import next_occurrence, seconds_until


def test_next_occurrence_later_today():
    now = datetime(2026, 7, 11, 8, 0, 0)
    assert next_occurrence(now, 12, 0) == datetime(2026, 7, 11, 12, 0, 0)


def test_next_occurrence_already_passed_rolls_tomorrow():
    now = datetime(2026, 7, 11, 13, 30, 0)
    assert next_occurrence(now, 12, 0) == datetime(2026, 7, 12, 12, 0, 0)


def test_next_occurrence_exactly_now_rolls_tomorrow():
    now = datetime(2026, 7, 11, 12, 0, 0)
    assert next_occurrence(now, 12, 0) == datetime(2026, 7, 12, 12, 0, 0)


def test_seconds_until_parses_hhmm():
    now = datetime(2026, 7, 11, 11, 0, 0)
    assert seconds_until(now, "12:00") == 3600.0


def test_seconds_until_rolls_over_midnight():
    now = datetime(2026, 7, 11, 19, 0, 0)
    # 12:00 already passed -> tomorrow noon = 17h away
    assert seconds_until(now, "12:00") == 17 * 3600.0
