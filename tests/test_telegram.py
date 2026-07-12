"""Tests for bot.telegram_bot pure helpers — sensor/blackout resolution with
visibility gating, sort flags, the registration-token HMAC, digest building,
and the small formatting helpers. No network: the PTB Application builds
offline and we never start polling.
"""
import time

import pytest

from bot import telegram_bot as tb
from bot import config


CREDS = """
telegram:
  token: "123:ABC"
  group_id: -100
mqtt:
  host: "broker"
  port: 1883
groups:
  ops: [1, 2]
  other: [3]
"""

DEFAULTS = """
defaults:
  interval: 300
devices:
  SM1:
    topic: "t/sm1"
    viewers: [ops]
    fields:
      T: {}
      H: {}
  SM2:
    topic: "t/sm2"
    viewers: [other]
    fields:
      T: {}
      H: {}
blackouts:
  R2:
    fields: [SM1_T]
    below: 0.5
    for_seconds: 10
    stale_after: 15
"""


@pytest.fixture
def bot(tmp_path, temp_db):
    sd = tmp_path / "sensors.d"
    sd.mkdir()
    (sd / "00-defaults.yaml").write_text(DEFAULTS)
    cf = tmp_path / "credentials.yaml"
    cf.write_text(CREDS)
    cfg = config.load(str(sd), str(cf))
    return tb.TelegramBot(cfg)


# --- module formatting helpers ---

def test_fmt_ago():
    assert tb._fmt_ago(30) == "30s"
    assert tb._fmt_ago(90) == "1m"
    assert tb._fmt_ago(7200) == "2h"
    assert tb._fmt_ago(172800) == "2d"


def test_fmt_bytes():
    assert tb._fmt_bytes(0) == "0 B"
    assert tb._fmt_bytes(1536) == "1.5 KB"
    assert tb._fmt_bytes(5 * 1024 * 1024) == "5.0 MB"


# --- sensor resolution + visibility ---

def test_resolve_sensors_wildcard_respects_visibility(bot):
    assert bot._resolve_sensors(["*"], user_id=1) == ["SM1_T", "SM1_H"]
    assert bot._resolve_sensors(["*"], user_id=3) == ["SM2_T", "SM2_H"]
    assert bot._resolve_sensors(["*"], user_id=99) == []


def test_resolve_sensors_exact_and_hidden(bot):
    assert bot._resolve_sensors(["SM1_T"], user_id=1) == ["SM1_T"]
    # SM2 is not visible to an ops user
    assert bot._resolve_sensors(["SM2_T"], user_id=1) == []


def test_resolve_sensors_glob_comma_dedup_caseinsensitive(bot):
    assert bot._resolve_sensors(["SM1_*"], user_id=1) == ["SM1_T", "SM1_H"]
    assert bot._resolve_sensors(["SM1_T,SM1_H"], user_id=1) == ["SM1_T", "SM1_H"]
    # duplicate pattern doesn't duplicate output
    assert bot._resolve_sensors(["SM1_T", "SM1_T"], user_id=1) == ["SM1_T"]
    assert bot._resolve_sensors(["sm1_t"], user_id=1) == ["SM1_T"]


def test_resolve_blackouts_viewer_gated(bot):
    assert bot._resolve_blackouts(["*"], user_id=1) == ["R2"]   # ops views SM1_T
    assert bot._resolve_blackouts(["*"], user_id=99) == []


# --- sort flags ---

def test_extract_sort(bot):
    assert bot._extract_sort(["-s", "SM1_T"]) == (["SM1_T"], "-s")
    assert bot._extract_sort(["SM1_T"]) == (["SM1_T"], None)
    # last flag wins
    assert bot._extract_sort(["-f", "-s"]) == ([], "-s")


def test_apply_sort_alphabetical(bot):
    names = ["SM2_T", "SM1_H", "SM1_T", "SM2_H"]
    assert bot._apply_sort(names, "-s") == ["SM1_H", "SM1_T", "SM2_H", "SM2_T"]


def test_apply_sort_by_field(bot):
    # default groups by field suffix (H before T), then by name
    names = ["SM1_T", "SM2_H", "SM1_H", "SM2_T"]
    assert bot._apply_sort(names, None) == ["SM1_H", "SM2_H", "SM1_T", "SM2_T"]


# --- registration token (HMAC) ---

def test_token_roundtrip(bot):
    tok = bot._make_token(42)
    assert bot._verify_token(tok, 42) is True


def test_token_rejects_wrong_sender(bot):
    tok = bot._make_token(42)
    assert bot._verify_token(tok, 43) is False


def test_token_rejects_tampered_signature(bot):
    tok = bot._make_token(42)
    tampered = tok[:-1] + ("A" if tok[-1] != "A" else "B")
    assert bot._verify_token(tampered, 42) is False


def test_token_rejects_malformed(bot):
    assert bot._verify_token("garbage", 42) is False
    assert bot._verify_token("", 42) is False


def test_token_rejects_expired(bot, monkeypatch):
    tok = bot._make_token(42)
    # jump forward > 24h
    monkeypatch.setattr(tb.time, "time", lambda: time.time() + 86400 * 2)
    assert bot._verify_token(tok, 42) is False


# --- digest building ---

def test_build_digest_only_subscribed_and_visible(bot, temp_db):
    now = int(time.time())
    temp_db.insert_reading("SM1_T", 21.0, ts=now)
    temp_db.insert_reading("SM1_H", 55.0, ts=now)
    temp_db.insert_reading("SM2_T", 19.0, ts=now)
    # user 1 (ops) subscribes to SM1_T (visible) and SM2_T (NOT visible)
    temp_db.subscribe_digest(1, "SM1_T")
    temp_db.subscribe_digest(1, "SM2_T")

    out = bot.build_digest(1)
    assert "SM1_T" in out
    assert "SM1_H" not in out      # not subscribed
    assert "SM2_T" not in out      # subscribed but not visible


def test_build_digest_empty_when_no_subscriptions(bot):
    assert bot.build_digest(1) == ""
