"""Tests for bot.telegram_bot, in two layers.

Pure helpers first — sensor/blackout resolution with visibility gating, sort
flags, staleness, the registration-token HMAC, digest building, the formatting
helpers — then the command handlers driven end to end against a fake bot app
(`_fake_app` records what was sent), which is where the permission gates,
argument parsing, name resolution and DB side effects actually live.

No network: the PTB Application builds offline and polling is never started.
"""
import asyncio
import logging
import time
from types import SimpleNamespace

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
  watchers: [4]
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
  SM3:
    topic: "t/sm3"
    admins: [ops]
    viewers: [watchers]
    fields:
      IF: {signal: true, topic: "t/sm3fast", json_path: cur}
blackouts:
  R2:
    fields: [SM1_T]
    below: 0.5
    for_seconds: 10
    stale_after: 15
  SIG:
    fields: [SM3_IF]
    below: 0.5
    for_seconds: 0
    stale_after: 9
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


# --- alarm band ordering guard ---

def test_threshold_order_ok_when_high_above_low():
    assert tb._threshold_order_error(high=30.0, low=10.0) is None


def test_threshold_order_ignores_missing_thresholds():
    # a threshold not yet set can never form an inverted band
    assert tb._threshold_order_error(high=None, low=10.0) is None
    assert tb._threshold_order_error(high=5.0, low=None) is None
    assert tb._threshold_order_error(high=None, low=None) is None


def test_threshold_order_rejects_inverted_band():
    err = tb._threshold_order_error(high=10.0, low=30.0)
    assert err is not None
    assert "10" in err and "30" in err


def test_threshold_order_rejects_equal_band():
    # equal thresholds leave no coherent band → rejected
    assert tb._threshold_order_error(high=20.0, low=20.0) is not None


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
    # ops (user 1) views SM1_T (R2) and is admin of SM3_IF (SIG)
    assert bot._resolve_blackouts(["*"], user_id=1) == ["R2", "SIG"]
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
    # default (None) and -f group by measured quantity (H before T), then name
    names = ["SM1_T", "SM2_H", "SM1_H", "SM2_T"]
    field_grouped = ["SM1_H", "SM2_H", "SM1_T", "SM2_T"]
    assert bot._apply_sort(names, None) == field_grouped
    assert bot._apply_sort(names, "-f") == field_grouped


def test_apply_sort_groups_multisegment_field_with_quantity(bot):
    # a multi-part field key (UPS_cip_T on device UPS) groups under "T" with the
    # plain _T sensors, not as a separate "cip_T" field
    names = ["UPS_cip_T", "SM1_UTA1_T", "UPS_ciop_T", "DK1_B"]
    # within the "T" group, tie-break by name ("ciop" < "cip": 'o' < 'p')
    assert bot._apply_sort(names, None) == [
        "DK1_B", "SM1_UTA1_T", "UPS_ciop_T", "UPS_cip_T",
    ]


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


# --- staleness of the "min ago" column (same rule as the offline alarm) ---

AVAIL_DEFAULTS = """
defaults:
  interval: 300
devices:
  FAST:
    topic: "t/fast"
    interval: 60
    viewers: [ops]
    fields:
      T: {}
  SLOW:
    topic: "t/slow"
    interval: 3600
    viewers: [ops]
    fields:
      T: {}
  ZB:
    topic: "t/zb"
    interval: 60
    availabilityTopic: "zigbee2mqtt/ZB/availability"
    viewers: [ops]
    fields:
      T: {}
"""


@pytest.fixture
def abot(tmp_path, temp_db):
    sd = tmp_path / "sensors.d"
    sd.mkdir()
    (sd / "00-defaults.yaml").write_text(AVAIL_DEFAULTS)
    cf = tmp_path / "credentials.yaml"
    cf.write_text(CREDS)
    return tb.TelegramBot(config.load(str(sd), str(cf)))


def test_stale_threshold_follows_each_sensor_interval(abot, temp_db):
    # 2h old: past 3×60s for FAST, well inside 3×3600s for SLOW
    old = int(time.time()) - 7200
    temp_db.insert_reading("FAST_T", 20.0, ts=old)
    temp_db.insert_reading("SLOW_T", 21.0, ts=old)
    out = abot._render_sensors_text(["FAST_T", "SLOW_T"])
    fast, slow = [ln for ln in out.splitlines() if ln.startswith(("FAST_T", "SLOW_T"))]
    assert fast.endswith("∞")
    assert slow.endswith("120")


def test_zigbee_availability_wins_over_data_cadence(abot, temp_db):
    # reading far older than 3×interval, but z2m says the device is online
    temp_db.insert_reading("ZB_T", 20.0, ts=int(time.time()) - 86400)
    abot.availability_snapshot_fn = lambda: {"ZB": True}
    assert "∞" not in abot._render_sensors_text(["ZB_T"])
    # and offline marks it silent even with a reading seconds old
    temp_db.insert_reading("ZB_T", 20.0)
    abot.availability_snapshot_fn = lambda: {"ZB": False}
    assert "∞" in abot._render_sensors_text(["ZB_T"])


def test_availability_ignored_for_device_without_topic(abot, temp_db):
    # a stray availability entry must not override the cadence rule for a
    # device that publishes no availability topic
    temp_db.insert_reading("FAST_T", 20.0, ts=int(time.time()) - 7200)
    abot.availability_snapshot_fn = lambda: {"FAST": True}
    assert "∞" in abot._render_sensors_text(["FAST_T"])


def test_stale_column_without_availability_hook(abot, temp_db):
    # no hook wired (e.g. tests, or before main.py binds it) -> cadence only
    temp_db.insert_reading("ZB_T", 20.0, ts=int(time.time()) - 7200)
    assert "∞" in abot._render_sensors_text(["ZB_T"])


# --- /listSignal rendering (pure) ---

def test_listsignal_admin_sees_live_signal_value(bot):
    # user 1 is admin of SM3 (ops) -> sees the live cached value of SM3_IF
    bot.signal_snapshot_fn = lambda: {"SM3_IF": {"value": 0.42, "ts": int(time.time())}}
    out = bot._render_signal_list(1)
    assert "⚡ SIG" in out
    assert "SM3_IF = 0.42" in out
    assert "🔕 not subscribed" in out
    assert "/digest SIG on" in out
    # R2 watches SM1_T which user 1 can view -> also listed, no signal rows
    assert "⚡ R2" in out


def test_listsignal_viewer_hides_live_value(bot):
    # user 4 (watchers) is a viewer of SM3 but not an admin -> name only, no value
    bot.signal_snapshot_fn = lambda: {"SM3_IF": {"value": 0.42, "ts": int(time.time())}}
    out = bot._render_signal_list(4)
    assert "SM3_IF" in out
    assert "0.42" not in out


def test_listsignal_subscription_state_flips_hint(bot, temp_db):
    temp_db.subscribe_digest(1, "SIG")
    out = bot._render_signal_list(1)
    assert "🔔 subscribed" in out
    assert "/digest SIG off" in out


def test_listsignal_none_for_outsider(bot):
    assert bot._render_signal_list(99) == "No blackout detection visible to you."


# --- /sysinfo ---

def test_render_sysinfo(bot, temp_db):
    bot.last_mqtt_fn = lambda: int(time.time()) - 5
    out = bot._render_sysinfo()
    assert f"v{tb.__version__}" in out
    assert "uptime:" in out
    assert "ultimo MQTT: 5s fa" in out
    assert "device: 3" in out          # SM1, SM2, SM3
    assert "sensori: 4" in out         # SM1_T/H, SM2_T/H (SM3_IF is a signal)
    assert "DB:" in out                # temp_db file exists


def test_render_sysinfo_no_mqtt(bot):
    bot.last_mqtt_fn = lambda: None
    assert "ultimo MQTT: mai" in bot._render_sysinfo()


def test_render_sysinfo_surfaces_config_warnings(bot):
    # A non-fatal config complaint must reach a human somewhere; a log line in a
    # container nobody tails does not count.
    assert "⚠️" not in bot._render_sysinfo()
    bot._cfg.warnings = ["SM1.H: declares 'admins' but not 'viewers' — ..."]
    assert "⚠️ config: SM1.H" in bot._render_sysinfo()


# --- unknown command ---

def _fake_app(sent, photos=None, docs=None):
    # message ids are handed out per chat, exactly as Telegram does it: two
    # chats see the same ids, which is what the prompt bookkeeping must survive
    next_id = {}

    async def send_message(chat_id, text, **kw):
        sent.append((chat_id, text))
        next_id[chat_id] = next_id.get(chat_id, 1000) + 1
        return SimpleNamespace(message_id=next_id[chat_id])

    async def send_photo(chat_id, photo, caption=None, **kw):
        if photos is not None:
            photos.append((chat_id, caption))

    async def send_document(chat_id, document, filename=None, **kw):
        if docs is not None:
            docs.append((chat_id, filename))

    async def delete_message(chat_id, message_id, **kw):
        pass

    return SimpleNamespace(bot=SimpleNamespace(
        send_message=send_message,
        send_photo=send_photo,
        send_document=send_document,
        delete_message=delete_message,
    ))


def _cmd_update(text, user_id):
    return SimpleNamespace(
        effective_message=SimpleNamespace(text=text),
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
    )


def test_unknown_command_replies_to_registered(bot, temp_db):
    sent = []
    bot._app = _fake_app(sent)
    bot._bot_username = "lortebot"
    temp_db.register_dm(1)
    asyncio.run(bot._cmd_unknown(_cmd_update("/foobar", 1), None))
    assert len(sent) == 1 and "unknown" in sent[0][1].lower()


def test_unknown_command_addressed_to_us_replies(bot, temp_db):
    sent = []
    bot._app = _fake_app(sent)
    bot._bot_username = "LorTeBot"          # case-insensitive match
    temp_db.register_dm(1)
    asyncio.run(bot._cmd_unknown(_cmd_update("/foobar@lortebot", 1), None))
    assert len(sent) == 1


def test_unknown_command_ignores_other_bot(bot, temp_db):
    sent = []
    bot._app = _fake_app(sent)
    bot._bot_username = "lortebot"
    temp_db.register_dm(1)
    asyncio.run(bot._cmd_unknown(_cmd_update("/foobar@otherbot", 1), None))
    assert sent == []


def test_unknown_command_ignores_unregistered(bot, temp_db):
    sent = []
    bot._app = _fake_app(sent)
    bot._bot_username = "lortebot"
    asyncio.run(bot._cmd_unknown(_cmd_update("/foobar", 5), None))   # 5 not registered
    assert sent == []


# --- command handlers end-to-end (auth, arg parsing, DB side effects) ---
#
# These exercise the actual /setAlarm, /clearAlarm, /ackOff, /forgetSensor
# handlers — not just the pure helpers — because that is where the auth
# checks, case-insensitive name resolution, and DB writes live.

HCREDS = """
telegram:
  token: "123:ABC"
  group_id: -100
mqtt:
  host: "broker"
  port: 1883
groups:
  ops: [1, 2]
  watchers: [4]
  other: [7]
superadmin: [9]
"""

HDEFAULTS = """
defaults:
  interval: 300
devices:
  SM1:
    topic: "t/sm1"
    admins: [ops]
    viewers: [watchers]
    fields:
      T: {}
      H: {}
  SM2:
    topic: "t/sm2"
    admins: [other]
    fields:
      T: {}
"""


@pytest.fixture
def hbot(tmp_path, temp_db):
    sd = tmp_path / "sensors.d"
    sd.mkdir()
    (sd / "00-defaults.yaml").write_text(HDEFAULTS)
    cf = tmp_path / "credentials.yaml"
    cf.write_text(HCREDS)
    b = tb.TelegramBot(config.load(str(sd), str(cf)))
    return b


def _ctx(*args):
    return SimpleNamespace(args=list(args))


ADMIN, VIEWER, OUTSIDER, SUPER = 1, 4, 99, 9   # per ops/watchers/superadmin above


def _run(bot, handler, user_id, *args):
    sent = []
    bot._app = _fake_app(sent)
    asyncio.run(handler(_cmd_update("/x", user_id), _ctx(*args)))
    return sent


# /setAlarm

def test_setalarm_admin_sets_threshold(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_setalarm, ADMIN, "SM1_T", "30")
    assert temp_db.get_threshold("SM1_T") == 30.0
    assert "updated" in sent[-1][1].lower()


def test_setalarm_case_insensitive_sensor(hbot, temp_db):
    _run(hbot, hbot._cmd_setalarm, ADMIN, "sm1_t", "30")
    assert temp_db.get_threshold("SM1_T") == 30.0


def test_setalarm_viewer_not_authorized(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_setalarm, VIEWER, "SM1_T", "30")
    assert temp_db.get_threshold("SM1_T") is None
    assert "authorized" in sent[-1][1].lower()


def test_setalarm_outsider_unknown_sensor(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_setalarm, OUTSIDER, "SM1_T", "30")
    assert "unknown" in sent[-1][1].lower()


def test_setalarm_non_numeric_rejected(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_setalarm, ADMIN, "SM1_T", "abc")
    assert temp_db.get_threshold("SM1_T") is None
    assert "number" in sent[-1][1].lower()


def test_setalarm_rejects_inverted_band(hbot, temp_db):
    temp_db.set_threshold_low("SM1_T", 50.0)
    sent = _run(hbot, hbot._cmd_setalarm, ADMIN, "SM1_T", "10")   # high < low
    assert temp_db.get_threshold("SM1_T") is None
    assert sent  # an error was sent


# /clearAlarm

def test_clearalarm_admin_clears(hbot, temp_db):
    temp_db.set_threshold("SM1_T", 30.0)
    _run(hbot, hbot._cmd_clearalarm, ADMIN, "SM1_T")
    assert temp_db.get_threshold("SM1_T") is None


def test_clearalarm_viewer_not_authorized(hbot, temp_db):
    temp_db.set_threshold("SM1_T", 30.0)
    _run(hbot, hbot._cmd_clearalarm, VIEWER, "SM1_T")
    assert temp_db.get_threshold("SM1_T") == 30.0   # untouched


# /ackOff

def test_ackoff_admin_silences(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_ackoff, ADMIN, "SM1")
    assert temp_db.is_silenced("SM1") is True
    assert "acknowledged" in sent[-1][1].lower()


def test_ackoff_case_insensitive_device(hbot, temp_db):
    _run(hbot, hbot._cmd_ackoff, ADMIN, "sm1")
    assert temp_db.is_silenced("SM1") is True


def test_ackoff_viewer_not_authorized(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_ackoff, VIEWER, "SM1")
    assert temp_db.is_silenced("SM1") is False
    assert "authorized" in sent[-1][1].lower()


def test_ackoff_unknown_device(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_ackoff, ADMIN, "NOPE")
    assert "unknown" in sent[-1][1].lower()


def test_ackoff_does_not_leak_device_existence(hbot, temp_db):
    # OUTSIDER is in no access group: a real device key and an invented one must
    # be indistinguishable, or /ackOff becomes a device-key oracle
    real = _run(hbot, hbot._cmd_ackoff, OUTSIDER, "SM1")
    fake = _run(hbot, hbot._cmd_ackoff, OUTSIDER, "NOPE")
    assert real[-1][1] == fake[-1][1]
    assert "unknown device" in real[-1][1].lower()
    assert temp_db.is_silenced("SM1") is False


def test_ackoff_viewer_told_it_lacks_admin(hbot, temp_db):
    # someone who already sees the device is told the truth: not an access leak,
    # they know it exists
    sent = _run(hbot, hbot._cmd_ackoff, VIEWER, "SM1")
    assert "authorized" in sent[-1][1].lower()


def test_ackoff_no_args_lists_active(hbot, temp_db):
    temp_db.silence_sensor("SM1")
    sent = _run(hbot, hbot._cmd_ackoff, ADMIN)
    assert "SM1" in sent[-1][1] and "active" in sent[-1][1].lower()


def test_ackoff_no_args_empty(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_ackoff, ADMIN)
    assert "no active" in sent[-1][1].lower()


def test_ackoff_no_args_scoped_to_visible_devices(hbot, temp_db):
    # ADMIN is in `ops` (SM1 only). SM2 belongs to `other` and must not appear —
    # the listing is a read like any other, so it obeys visibility.
    temp_db.silence_sensor("SM1")
    temp_db.silence_sensor("SM2")
    body = _run(hbot, hbot._cmd_ackoff, ADMIN)[-1][1]
    assert "SM1" in body and "SM2" not in body


def test_ackoff_no_args_viewer_sees_own_device(hbot, temp_db):
    # VIEWER is only a viewer of SM1, never its admin: enough to see the ack.
    temp_db.silence_sensor("SM1")
    assert "SM1" in _run(hbot, hbot._cmd_ackoff, VIEWER)[-1][1]


def test_ackoff_no_args_superadmin_sees_everything(hbot, temp_db):
    # SUPER is in no Access Group, so this is the one listing that ignores
    # visibility — a caretaker view of the whole installation.
    temp_db.silence_sensor("SM1")
    temp_db.silence_sensor("SM2")
    body = _run(hbot, hbot._cmd_ackoff, SUPER)[-1][1]
    assert "SM1" in body and "SM2" in body


def test_ackoff_no_args_outsider_not_authorized(hbot, temp_db):
    # Anyone can DM the bot and be registered, so the listing must check group
    # membership itself or it leaks device keys to a passer-by.
    temp_db.silence_sensor("SM1")
    body = _run(hbot, hbot._cmd_ackoff, OUTSIDER)[-1][1]
    assert "authorized" in body.lower() and "SM1" not in body


# /forgetSensor

def test_forgetsensor_requires_superadmin(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_forgetsensor, ADMIN, "SM1")   # admin, not superadmin
    assert "authorized" in sent[-1][1].lower()


def test_forgetsensor_superadmin_case_insensitive(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_forgetsensor, SUPER, "sm1")
    assert "archived" in sent[-1][1].lower()


# /setAlarmLow

def test_setalarmlow_admin_sets(hbot, temp_db):
    _run(hbot, hbot._cmd_setalarmlow, ADMIN, "SM1_T", "10")
    assert temp_db.get_threshold_low("SM1_T") == 10.0


def test_setalarmlow_viewer_not_authorized(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_setalarmlow, VIEWER, "SM1_T", "10")
    assert temp_db.get_threshold_low("SM1_T") is None
    assert "authorized" in sent[-1][1].lower()


def test_setalarmlow_rejects_inverted_band(hbot, temp_db):
    temp_db.set_threshold("SM1_T", 20.0)
    sent = _run(hbot, hbot._cmd_setalarmlow, ADMIN, "SM1_T", "50")   # low > high
    assert temp_db.get_threshold_low("SM1_T") is None
    assert sent


# /clearAlarmLow

def test_clearalarmlow_admin_clears(hbot, temp_db):
    temp_db.set_threshold_low("SM1_T", 10.0)
    _run(hbot, hbot._cmd_clearalarmlow, ADMIN, "SM1_T")
    assert temp_db.get_threshold_low("SM1_T") is None


def test_clearalarmlow_viewer_not_authorized(hbot, temp_db):
    temp_db.set_threshold_low("SM1_T", 10.0)
    _run(hbot, hbot._cmd_clearalarmlow, VIEWER, "SM1_T")
    assert temp_db.get_threshold_low("SM1_T") == 10.0


# /silent (per-user mutes)

def test_silent_mutes_for_hours(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_silent, ADMIN, "SM1_T", "3h")
    assert temp_db.is_muted(ADMIN, "SM1_T") is True
    assert "3h" in sent[-1][1]


def test_silent_hours_clamped_to_24(hbot, temp_db):
    now = int(time.time())
    _run(hbot, hbot._cmd_silent, ADMIN, "SM1_T", "99h")
    rows = temp_db.get_active_mutes(ADMIN)
    assert rows and rows[0]["until_ts"] - now <= 24 * 3600 + 5


def test_silent_unmute(hbot, temp_db):
    temp_db.mute_sensor(ADMIN, "SM1_T", int(time.time()) + 3600)
    _run(hbot, hbot._cmd_silent, ADMIN, "SM1_T")   # no Nh -> unmute
    assert temp_db.is_muted(ADMIN, "SM1_T") is False


def test_silent_unmute_reports_only_real_mutes(hbot, temp_db):
    # ADMIN sees SM1_T and SM1_H, only one of them is muted
    temp_db.mute_sensor(ADMIN, "SM1_T", int(time.time()) + 3600)
    sent = _run(hbot, hbot._cmd_silent, ADMIN, "SM1_*")
    assert "Unmuted 1 field(s)" in sent[-1][1]


def test_silent_unmute_nothing_muted(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_silent, ADMIN, "SM1_*")
    assert "No active mutes among 2 matching field(s)." in sent[-1][1]


def test_silent_unmute_expired_mute_is_not_counted(hbot, temp_db):
    temp_db.mute_sensor(ADMIN, "SM1_T", int(time.time()) - 1)
    sent = _run(hbot, hbot._cmd_silent, ADMIN, "SM1_T")
    assert "No active mutes" in sent[-1][1]


def test_silent_no_args_lists(hbot, temp_db):
    temp_db.mute_sensor(ADMIN, "SM1_T", int(time.time()) + 3600)
    sent = _run(hbot, hbot._cmd_silent, ADMIN)
    assert "SM1_T" in sent[-1][1] and "left" in sent[-1][1].lower()


def test_silent_is_per_user(hbot, temp_db):
    _run(hbot, hbot._cmd_silent, ADMIN, "SM1_T", "3h")
    assert temp_db.is_muted(VIEWER, "SM1_T") is False   # other user unaffected


# /graph, /csv, /xlsx — export handlers (files)

def _run_files(bot, handler, user_id, *args):
    sent, photos, docs = [], [], []
    bot._app = _fake_app(sent, photos, docs)
    asyncio.run(handler(_cmd_update("/x", user_id), _ctx(*args)))
    return sent, photos, docs


def test_graph_sends_photo(hbot, temp_db):
    for i in range(3):
        temp_db.insert_reading("SM1_T", 20.0 + i, int(time.time()) - i * 60)
    sent, photos, docs = _run_files(hbot, hbot._cmd_graph, ADMIN, "SM1_T")
    assert len(photos) == 1


def test_graph_hours_admin_clamped_to_72(hbot, temp_db):
    # admin gets 72h ceiling; a bogus 999h must not raise, just clamp
    temp_db.insert_reading("SM1_T", 20.0)
    sent, photos, docs = _run_files(hbot, hbot._cmd_graph, ADMIN, "SM1_T", "999h")
    assert len(photos) == 1


def test_csv_sends_document(hbot, temp_db):
    temp_db.insert_reading("SM1_T", 21.0)
    sent, photos, docs = _run_files(hbot, hbot._cmd_csv, ADMIN, "SM1_T")
    assert len(docs) == 1 and docs[0][1].endswith(".csv")


def test_csv_no_data_reports(hbot, temp_db):
    sent, photos, docs = _run_files(hbot, hbot._cmd_csv, ADMIN, "SM1_T")
    assert docs == [] and "no data" in sent[-1][1].lower()


def test_xlsx_sends_document(hbot, temp_db):
    temp_db.insert_reading("SM1_T", 21.0)
    sent, photos, docs = _run_files(hbot, hbot._cmd_xlsx, ADMIN, "SM1_T")
    assert len(docs) == 1 and docs[0][1].endswith(".xlsx")


def test_export_no_matching_sensor(hbot, temp_db):
    sent, photos, docs = _run_files(hbot, hbot._cmd_csv, ADMIN, "NOPE")
    assert docs == [] and "no matching" in sent[-1][1].lower()


# /digest (per-user subscriptions)

def test_digest_subscribe_on(hbot, temp_db):
    _run(hbot, hbot._cmd_digest, ADMIN, "SM1_T", "on")
    assert "SM1_T" in temp_db.get_digest_subscriptions(ADMIN)


def test_digest_unsubscribe_off(hbot, temp_db):
    temp_db.subscribe_digest(ADMIN, "SM1_T")
    _run(hbot, hbot._cmd_digest, ADMIN, "SM1_T", "off")
    assert "SM1_T" not in temp_db.get_digest_subscriptions(ADMIN)


def test_digest_no_args_lists_visible_only(hbot, temp_db):
    temp_db.subscribe_digest(ADMIN, "SM1_T")
    sent = _run(hbot, hbot._cmd_digest, ADMIN)
    assert "SM1_T" in sent[-1][1]


def test_digest_bad_usage(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_digest, ADMIN, "SM1_T")   # missing on|off
    assert "usage" in sent[-1][1].lower()


# /list, /get

def test_list_shows_device_reading(hbot, temp_db):
    temp_db.insert_reading("SM1_T", 22.5)
    sent = _run(hbot, hbot._cmd_list, ADMIN)
    assert "SM1" in sent[-1][1]


def test_list_renders_one_block_per_device(hbot, temp_db):
    temp_db.insert_reading("SM1_T", 22.5)
    temp_db.insert_reading("SM1_H", 55.0)
    body = _run(hbot, hbot._cmd_list, ADMIN)[-1][1]
    lines = body.splitlines()
    assert lines[0] == "```"                 # monospace, or the columns collapse
    assert "SM1" in lines                    # device key on its own line
    assert [l for l in lines if l.startswith("  T ")]   # fields indented under it


def test_list_thresholds_marked_by_direction(hbot, temp_db):
    temp_db.insert_reading("SM1_T", 22.5)
    temp_db.set_threshold("SM1_T", 30.0)
    temp_db.set_threshold_low("SM1_T", 5.0)
    body = _run(hbot, hbot._cmd_list, ADMIN)[-1][1]
    assert "\u25b3 30.0" in body and "\u25bd 5.0" in body


def test_list_field_without_reading_still_listed(hbot, temp_db):
    temp_db.insert_reading("SM1_T", 22.5)        # SM1_H never read
    body = _run(hbot, hbot._cmd_list, ADMIN)[-1][1]
    assert [l for l in body.splitlines() if l.startswith("  H ") and l.endswith("--")]


def test_list_splits_long_listing_and_closes_every_fence(hbot, temp_db):
    # a listing past the 4096-char limit is split; a chunk that ended mid-block
    # used to leave its code fence open, so Telegram rendered the rest as text
    for i in range(900):
        hbot._cfg.devices["SM1"].fields[f"F{i}"] = hbot._cfg.sensors["SM1_T"]
    sent = _run(hbot, hbot._cmd_list, ADMIN)
    assert len(sent) > 1
    assert all(len(text) <= 4096 for _, text in sent)
    for _, text in sent:
        assert text.count("```") % 2 == 0 and text.startswith("```")


def test_list_footer_escapes_the_sensor_name_example(hbot, temp_db):
    # the message is sent as Markdown for the code fence, so an unescaped
    # SM2_UTA1_T reached Telegram as italics and arrived as SM2UTA1T
    temp_db.insert_reading("SM1_T", 22.5)
    body = _run(hbot, hbot._cmd_list, ADMIN)[-1][1]
    assert "SM2\\_UTA1\\_T" in body and "device\\_field" in body


def test_list_empty_when_no_visible(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_list, OUTSIDER)   # sees nothing
    assert "no sensors" in sent[-1][1].lower()


def test_get_named_sensor(hbot, temp_db):
    temp_db.insert_reading("SM1_T", 22.5)
    sent = _run(hbot, hbot._cmd_get, ADMIN, "SM1_T")
    assert "SM1_T" in sent[-1][1]


def test_get_unknown_sensor(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_get, ADMIN, "NOPE")
    assert "no matching" in sent[-1][1].lower()


# /lastSeen

def test_lastseen_shows_timestamp_not_saturated(hbot, temp_db):
    # 3 days old: /get would print "∞", /lastSeen must still date it
    old = int(time.time()) - 3 * 86400
    temp_db.insert_reading("SM1_T", 22.5, old)
    sent = _run(hbot, hbot._cmd_lastseen, ADMIN, "SM1_T")
    assert "SM1_T" in sent[-1][1]
    assert tb._fmt_ts(old) in sent[-1][1] and "3d" in sent[-1][1]


def test_lastseen_no_args_lists_sensor_that_never_reported(hbot, temp_db):
    # no args = all visible sensors (not digest subs), silent ones included
    sent = _run(hbot, hbot._cmd_lastseen, ADMIN)
    assert "SM1_T" in sent[-1][1] and "never" in sent[-1][1]


def test_lastseen_no_matching_sensor(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_lastseen, ADMIN, "NOPE")
    assert "no matching" in sent[-1][1].lower()


def test_lastseen_hides_invisible_sensors(hbot, temp_db):
    temp_db.insert_reading("SM2_T", 30.0)
    sent = _run(hbot, hbot._cmd_lastseen, ADMIN, "*")
    assert "SM2_T" not in sent[-1][1]


# Signals are not Sensors — no threshold command may touch one
# (`bot` fixture: user 1 administers SM3, whose only field SM3_IF is a Signal)

def test_setalarm_rejects_signal_name(bot, temp_db):
    sent = _run(bot, bot._cmd_setalarm, 1, "SM3_IF", "30")
    assert "unknown sensor" in sent[-1][1].lower()
    assert temp_db.get_threshold("SM3_IF") is None


def test_setalarmlow_rejects_signal_name(bot, temp_db):
    sent = _run(bot, bot._cmd_setalarmlow, 1, "SM3_IF", "1")
    assert "unknown sensor" in sent[-1][1].lower()
    assert temp_db.get_threshold_low("SM3_IF") is None


def test_getalarm_rejects_signal_name(bot, temp_db):
    sent = _run(bot, bot._cmd_getalarm, 1, "SM3_IF")
    assert "unknown sensor" in sent[-1][1].lower()


def test_last5alarm_rejects_signal_name(bot, temp_db):
    sent = _run(bot, bot._cmd_last5alarm, 1, "SM3_IF")
    assert "unknown sensor" in sent[-1][1].lower()


def test_clearalarm_rejects_signal_name(bot, temp_db):
    sent = _run(bot, bot._cmd_clearalarm, 1, "SM3_IF")
    assert "unknown sensor" in sent[-1][1].lower()


# /getAlarm

def test_getalarm_named_shows_band(hbot, temp_db):
    temp_db.set_threshold("SM1_T", 30.0)
    temp_db.set_threshold_low("SM1_T", 10.0)
    sent = _run(hbot, hbot._cmd_getalarm, ADMIN, "SM1_T")
    assert "SM1_T" in sent[-1][1] and "10" in sent[-1][1] and "30" in sent[-1][1]


def test_getalarm_formats_like_the_value_columns(hbot, temp_db):
    # `:g` printed 30 where /get prints 30.0, and fell into scientific notation
    # on large numbers — the same threshold had two spellings across commands
    temp_db.set_threshold("SM1_T", 1234567.0)
    temp_db.set_threshold_low("SM1_T", 30.0)
    sent = _run(hbot, hbot._cmd_getalarm, ADMIN, "SM1_T")
    assert "30.0" in sent[-1][1]
    assert "1234567.0" in sent[-1][1] and "e+" not in sent[-1][1]


def test_getalarm_unknown_sensor(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_getalarm, ADMIN, "NOPE")
    assert "unknown" in sent[-1][1].lower()


# /lastAlarms, /last5Alarm

def test_lastalarms_reports_recent(hbot, temp_db):
    temp_db.insert_alarm("SM1_T", "ALARM", "SM1_T: hot")
    sent = _run(hbot, hbot._cmd_lastalarms, ADMIN, "SM1_T")
    assert "hot" in sent[-1][1]


def test_lastalarms_none(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_lastalarms, ADMIN, "SM1_T")
    assert "no alarms" in sent[-1][1].lower()


def test_lastalarms_hours_out_of_range(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_lastalarms, ADMIN, "SM1_T", "99h")
    assert "between 1 and 24" in sent[-1][1]


def test_lastalarms_includes_offline_of_owning_device(hbot, temp_db):
    # OFFLINE rows are recorded under the DEVICE key; a viewer of one of its
    # fields must still see them, or the offline history is unreachable
    temp_db.insert_alarm("SM1", "OFFLINE", "OFFLINE SM1: no data for >900s")
    sent = _run(hbot, hbot._cmd_lastalarms, ADMIN, "SM1_T")
    assert "no data for >900s" in sent[-1][1]
    assert "🔴" in sent[-1][1]


def test_lastalarms_hides_offline_of_invisible_device(hbot, temp_db):
    # SM2 belongs to the `other` group; ADMIN sees no field of it
    temp_db.insert_alarm("SM1", "OFFLINE", "OFFLINE SM1: no data for >900s")
    temp_db.insert_alarm("SM2", "OFFLINE", "OFFLINE SM2: no data for >900s")
    sent = _run(hbot, hbot._cmd_lastalarms, ADMIN, "*")
    assert "SM1" in sent[-1][1]      # listing is not empty…
    assert "SM2" not in sent[-1][1]  # …and still leaks nothing


def test_lastalarms_subscribed_blackout_group(bot, temp_db):
    # user 1 views SM1_T, which blackout group R2 watches
    temp_db.subscribe_digest(1, "R2")
    temp_db.insert_alarm("R2", "BLACKOUT", "⚡ BLACKOUT R2: no current for >10s")
    sent = _run(bot, bot._cmd_lastalarms, 1)
    assert "no current" in sent[-1][1]
    assert sent[-1][1].count("⚡") == 1      # marker not printed twice


def test_lastalarms_blackout_end_keeps_the_live_marker(bot, temp_db):
    # history must render a BLACKOUT_END with the same 🔌 the live message used,
    # and strip the stored one instead of printing it twice
    temp_db.subscribe_digest(1, "R2")
    temp_db.insert_alarm("R2", "BLACKOUT_END",
                         "🔌 BLACKOUT end after 1h 23m. (SM1 restored). SM1_T=2.0")
    sent = _run(bot, bot._cmd_lastalarms, 1)
    assert "BLACKOUT end after 1h 23m" in sent[-1][1]
    assert sent[-1][1].count("🔌") == 1
    assert "🟢" not in sent[-1][1]


def test_lastalarms_unsubscribed_blackout_group_excluded(bot, temp_db):
    temp_db.subscribe_digest(1, "SM1_T")
    temp_db.insert_alarm("SM1_T", "ALARM", "SM1_T: hot")
    temp_db.insert_alarm("R2", "BLACKOUT", "⚡ BLACKOUT R2: no current for >10s")
    sent = _run(bot, bot._cmd_lastalarms, 1)
    assert "hot" in sent[-1][1]              # the subscribed sensor is listed…
    assert "no current" not in sent[-1][1]   # …the unsubscribed group is not


def test_lastalarms_blackout_group_named_in_expr(bot, temp_db):
    temp_db.insert_alarm("R2", "BLACKOUT", "⚡ BLACKOUT R2: no current for >10s")
    sent = _run(bot, bot._cmd_lastalarms, 1, "R2")
    assert "no current" in sent[-1][1]


def test_lastalarms_blackout_group_hidden_from_outsider(bot, temp_db):
    temp_db.insert_alarm("R2", "BLACKOUT", "⚡ BLACKOUT R2: no current for >10s")
    sent = _run(bot, bot._cmd_lastalarms, 99, "R2")   # user 99 is in no group
    assert "no matching" in sent[-1][1].lower()       # refused before any query


def test_last5alarm_includes_device_offline(hbot, temp_db):
    temp_db.insert_alarm("SM1_T", "ALARM", "SM1_T: hot")
    temp_db.insert_alarm("SM1", "OFFLINE", "OFFLINE SM1: no data for >900s")
    sent = _run(hbot, hbot._cmd_last5alarm, ADMIN, "SM1_T")
    assert "hot" in sent[-1][1] and "no data for >900s" in sent[-1][1]


def test_last5alarm_named(hbot, temp_db):
    temp_db.insert_alarm("SM1_T", "ALARM", "SM1_T: hot")
    sent = _run(hbot, hbot._cmd_last5alarm, ADMIN, "SM1_T")
    assert "hot" in sent[-1][1]


def test_last5alarm_unknown_sensor(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_last5alarm, ADMIN, "NOPE")
    assert "unknown" in sent[-1][1].lower()


def test_last5alarm_blackout_group(bot, temp_db):
    # a Blackout Group id is an alarm subject like a sensor name is
    temp_db.insert_alarm("R2", "BLACKOUT", "\u26a1 BLACKOUT R2: no current for >10s")
    sent = _run(bot, bot._cmd_last5alarm, 1, "r2")   # case-insensitive
    assert "no current" in sent[-1][1]


def test_last5alarm_blackout_group_hidden_from_outsider(bot, temp_db):
    temp_db.insert_alarm("R2", "BLACKOUT", "\u26a1 BLACKOUT R2: no current for >10s")
    sent = _run(bot, bot._cmd_last5alarm, 99, "R2")   # user 99 is in no group
    assert "unknown" in sent[-1][1].lower()


# /usersActivity, /dbStats — superadmin only

def test_usersactivity_requires_superadmin(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_usersactivity, ADMIN)
    assert "authorized" in sent[-1][1].lower()


def test_usersactivity_lists(hbot, temp_db):
    temp_db.record_activity(2, "bob", "Bob")
    sent = _run(hbot, hbot._cmd_usersactivity, SUPER)
    assert "Bob" in sent[-1][1]


def test_dbstats_requires_superadmin(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_dbstats, ADMIN)
    assert "authorized" in sent[-1][1].lower()


def test_dbstats_renders(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_dbstats, SUPER)
    assert "DB stats" in sent[-1][1]


# /reloadConfig — superadmin only

def test_reloadconfig_requires_superadmin(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_reloadconfig, ADMIN)
    assert "authorized" in sent[-1][1].lower()


def test_reloadconfig_not_configured(hbot, temp_db):
    hbot._reload_fn = None
    sent = _run(hbot, hbot._cmd_reloadconfig, SUPER)
    assert "not configured" in sent[-1][1].lower()


def test_reloadconfig_success(hbot, temp_db):
    hbot._reload_fn = lambda: hbot._cfg   # reload returns a valid config
    sent = _run(hbot, hbot._cmd_reloadconfig, SUPER)
    assert "reloaded" in sent[-1][1].lower()


SIGNAL_CREDS = """
telegram:
  token: "123:ABC"
  group_id: -100
mqtt:
  host: "broker"
  port: 1883
groups:
  ops: [1, 2]
superadmin: [9]
"""


def _signal_defaults(admins: str) -> str:
    return f"""
defaults:
  interval: 300
devices:
  SM3:
    topic: "t/sm3"
    admins: [{admins}]
    fields:
      IF: {{signal: true, topic: "t/sm3fast", json_path: cur}}
blackouts:
  SIG:
    fields: [SM3_IF]
    below: 0.5
    for_seconds: 0
    stale_after: 9
"""


def test_reloadconfig_refreshes_signals(tmp_path, temp_db):
    # revoking access to a Signal must take effect on /reloadConfig: the Signal
    # table gates viewers_of/admins_of/is_signal, and left stale it kept showing
    # the live value to the revoked user until a process restart.
    sd = tmp_path / "sensors.d"
    sd.mkdir()
    (sd / "00-defaults.yaml").write_text(_signal_defaults("ops"))
    cf = tmp_path / "credentials.yaml"
    cf.write_text(SIGNAL_CREDS)
    b = tb.TelegramBot(config.load(str(sd), str(cf)))
    b.signal_snapshot_fn = lambda: {"SM3_IF": {"value": 0.42, "ts": int(time.time())}}
    assert "0.42" in b._render_signal_list(1)

    # config now grants the signal to nobody
    (sd / "00-defaults.yaml").write_text(_signal_defaults(""))
    b._reload_fn = lambda: config.load(str(sd), str(cf))
    _run(b, b._cmd_reloadconfig, SUPER)
    assert "0.42" not in b._render_signal_list(1)


# /start — DM registration + token gating

def _start_update(user_id, args_chat_sent):
    async def send_message(text, **kw):
        args_chat_sent.append(text)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id, send_message=send_message),
    )


def test_start_no_args_registers(hbot, temp_db):
    hbot._app = _fake_app([])
    chat_sent = []
    asyncio.run(hbot._cmd_start(_start_update(7, chat_sent), _ctx()))
    assert temp_db.is_dm_registered(7) is True
    assert "activated" in chat_sent[-1].lower()


def test_start_valid_token_registers(hbot, temp_db):
    token = hbot._make_token(7)
    chat_sent = []
    asyncio.run(hbot._cmd_start(_start_update(7, chat_sent), _ctx(token)))
    assert temp_db.is_dm_registered(7) is True
    assert "registration complete" in chat_sent[-1].lower()


def test_start_wrong_token_does_not_register(hbot, temp_db):
    token = hbot._make_token(7)
    chat_sent = []
    asyncio.run(hbot._cmd_start(_start_update(8, chat_sent), _ctx(token)))  # sender != 7
    assert temp_db.is_dm_registered(8) is False
    assert chat_sent == []


# _on_arg_reply — ForceReply follow-up routing (browser path via _pending)

def _reply_update(user_id, text):
    # _on_arg_reply reads update.effective_message (resolves edited_message too).
    return SimpleNamespace(
        effective_message=SimpleNamespace(text=text, reply_to_message=None),
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
    )


def test_on_arg_reply_routes_pending_to_csv(hbot, temp_db):
    temp_db.insert_reading("SM1_T", 21.0)
    sent, docs = [], []
    hbot._app = _fake_app(sent, None, docs)
    hbot._pending[ADMIN] = ("csv", time.time(), 111)
    ctx = SimpleNamespace(args=[])
    asyncio.run(hbot._on_arg_reply(_reply_update(ADMIN, "SM1_T"), ctx))
    assert len(docs) == 1


def test_on_arg_reply_ignores_expired_pending(hbot, temp_db):
    sent, docs = [], []
    hbot._app = _fake_app(sent, None, docs)
    hbot._pending[ADMIN] = ("csv", time.time() - 999, 111)   # stale
    ctx = SimpleNamespace(args=[])
    asyncio.run(hbot._on_arg_reply(_reply_update(ADMIN, "SM1_T"), ctx))
    assert docs == [] and sent == []


def test_on_arg_reply_consumes_edited_argument(hbot, temp_db):
    # effective_message, not message: on Telegram Web the ForceReply argument can
    # arrive as an edit (edited_message). It must still dispatch the pending
    # command — with the old `update.message` read it silently did nothing.
    temp_db.insert_reading("SM1_T", 21.0)
    sent, docs = [], []
    hbot._app = _fake_app(sent, None, docs)
    hbot._pending[ADMIN] = ("csv", time.time(), 111)          # ADMIN == _real_update's user id
    ctx = SimpleNamespace(args=[])
    edited = _real_update(hbot, "SM1_T", edited=True, command=False)
    asyncio.run(hbot._on_arg_reply(edited, ctx))
    assert len(docs) == 1


def _prompt_reply_update(user_id, text, prompt_id, chat_id=None):
    """A phone-style reply: the argument arrives as a reply to the prompt."""
    return SimpleNamespace(
        effective_message=SimpleNamespace(
            text=text,
            reply_to_message=SimpleNamespace(message_id=prompt_id),
        ),
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id if chat_id is not None else user_id),
    )


def test_arg_prompt_is_scoped_to_its_chat(hbot, temp_db):
    # message ids are per-chat counters, so two users' DMs hand out the same id:
    # replying in one chat must never dispatch the other chat's pending command
    temp_db.insert_reading("SM1_T", 21.0)
    sent, docs = [], []
    hbot._app = _fake_app(sent, None, docs)
    asyncio.run(hbot._prompt_args(VIEWER, "csv"))     # VIEWER's prompt, id 1001
    sent.clear()
    # ADMIN replies in their own chat to a message with the very same id
    asyncio.run(hbot._on_arg_reply(_prompt_reply_update(ADMIN, "SM1_T", 1001), _ctx()))
    assert docs == [] and sent == []                  # nothing dispatched for ADMIN
    assert len(hbot._arg_prompts) == 1                # VIEWER's prompt survives


def test_answering_older_prompt_keeps_newer_one_alive(hbot, temp_db):
    # two prompts open: answering the older by reply must not discard the
    # fallback tracker of the newer, whose plain-text answer would then vanish
    temp_db.insert_reading("SM1_T", 21.0)
    hbot._arg_prompts[(ADMIN, 111)] = "csv"      # older prompt
    hbot._arg_prompts[(ADMIN, 222)] = "graph"    # newer prompt
    hbot._pending[ADMIN] = ("graph", time.time(), 222)
    sent, photos, docs = [], [], []
    hbot._app = _fake_app(sent, photos, docs)
    asyncio.run(hbot._on_arg_reply(_prompt_reply_update(ADMIN, "SM1_T", 111), _ctx()))
    assert len(docs) == 1                        # older prompt answered -> csv
    assert hbot._pending.get(ADMIN) is not None   # newer prompt still tracked
    # …and the browser fallback for it still works
    asyncio.run(hbot._on_arg_reply(_reply_update(ADMIN, "SM1_T"), _ctx()))
    assert len(photos) == 1


# anonymous group admins — updates with no effective_user

def _anonymous_update(text="/get"):
    return SimpleNamespace(
        effective_message=SimpleNamespace(text=text, reply_to_message=None),
        effective_user=None,
        effective_chat=SimpleNamespace(id=-100),
    )


@pytest.mark.parametrize("handler_name", [
    "_cmd_get", "_cmd_list", "_cmd_start", "_cmd_myid", "_cmd_help", "_on_arg_reply",
])
def test_anonymous_sender_does_not_crash(hbot, temp_db, handler_name):
    # a group with anonymous admins sends updates whose effective_user is None;
    # dereferencing it raised AttributeError and the command died unanswered
    sent = []
    hbot._app = _fake_app(sent)
    asyncio.run(getattr(hbot, handler_name)(_anonymous_update(), _ctx()))
    assert sent == []


# long listings are split instead of exceeding Telegram's 4096-char limit

def test_usersactivity_splits_long_listing(hbot, temp_db):
    for i in range(300):
        temp_db.record_activity(1000 + i, f"user{i}", f"A Fairly Long Display Name {i}")
    sent = _run(hbot, hbot._cmd_usersactivity, SUPER)
    assert len(sent) > 1
    assert all(len(text) <= 4096 for _, text in sent)
    joined = "\n".join(t for _, t in sent)
    assert "user0" in joined and "user299" in joined   # nothing dropped


# notify_* — DM gating (registration / mute / subscription)

def test_notify_sensor_gated_by_registration_and_mute(hbot, temp_db):
    sent = []
    hbot._app = _fake_app(sent)
    # ADMIN(1) viewer of SM1_T; register only ADMIN
    temp_db.register_dm(ADMIN)
    asyncio.run(hbot.notify_sensor("SM1_T", "hot"))
    assert [c for c, _ in sent] == [ADMIN]

    sent.clear()
    temp_db.mute_sensor(ADMIN, "SM1_T", int(time.time()) + 3600)
    asyncio.run(hbot.notify_sensor("SM1_T", "hot"))
    assert sent == []   # muted -> suppressed


def test_notify_device_requires_subscription(hbot, temp_db):
    sent = []
    hbot._app = _fake_app(sent)
    temp_db.register_dm(ADMIN)
    asyncio.run(hbot.notify_device("SM1", "offline"))
    assert sent == []                       # registered but not subscribed

    temp_db.subscribe_digest(ADMIN, "SM1_T")
    asyncio.run(hbot.notify_device("SM1", "offline"))
    assert [c for c, _ in sent] == [ADMIN]


# /help — sections gated by role

def test_help_viewer_has_no_admin_section(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_help, VIEWER)
    body = sent[-1][1]
    assert "Admin commands" not in body and "Superadmin commands" not in body


def test_help_admin_sees_admin_section(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_help, ADMIN)
    body = sent[-1][1]
    assert "Admin commands" in body and "Superadmin commands" not in body


def test_help_superadmin_sees_superadmin_section(hbot, temp_db):
    # SUPER(9) is superadmin but not in any admin group, so only the
    # superadmin section is appended (admin section is gated on is_any_admin).
    sent = _run(hbot, hbot._cmd_help, SUPER)
    assert "Superadmin commands" in sent[-1][1]


# /exprSyntax, /listSignal — thin wrappers, smoke

def test_exprsyntax_replies(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_exprsyntax, ADMIN)
    assert sent and sent[-1][1]


def test_listsignal_replies(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_listsignal, ADMIN)
    assert sent and sent[-1][1]


# --- /help vs the autocomplete menu vs the registered handlers ---
#
# The menu (set_my_commands) is deliberately user-level only: admin and
# superadmin commands still work when typed but stay out of autocomplete.
# These tests pin that split so a new *user* command can't silently miss the
# menu (which is how /listSignal was lost).

# Every command whose handler gates on is_admin / is_superadmin. Anything
# registered but absent from the menu must be in here.
MENU_EXEMPT = {
    "setalarm", "setalarmlow", "clearalarm", "clearalarmlow",
    "ackoff", "forgetsensor", "reloadconfig", "usersactivity", "dbstats",
}


def _registered_commands(bot):
    from telegram.ext import CommandHandler
    out = set()
    for group in bot._app.handlers.values():
        for h in group:
            if isinstance(h, CommandHandler):
                out |= set(h.commands)
    return out


def _menu_commands(bot):
    captured = []

    async def set_my_commands(cmds):
        captured.extend(cmds)

    real_app = bot._app
    bot._app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=set_my_commands))
    try:
        asyncio.run(bot._set_user_commands())
    finally:
        bot._app = real_app   # other assertions still need the real handlers
    return captured


def test_menu_commands_are_valid_telegram_names(bot):
    import re
    for c in _menu_commands(bot):
        assert re.fullmatch(r"[a-z0-9_]{1,32}", c.command), c.command
        assert 0 < len(c.description) <= 256, c.command


def test_menu_has_no_duplicates(bot):
    names = [c.command for c in _menu_commands(bot)]
    assert len(names) == len(set(names))


def test_every_menu_command_has_a_handler(bot):
    menu = {c.command for c in _menu_commands(bot)}
    assert menu <= _registered_commands(bot)


def test_menu_omits_exactly_the_privileged_commands(bot):
    # The regression guard: a newly added user-level command that never made it
    # into set_my_commands shows up here as an unexpected omission.
    menu = {c.command for c in _menu_commands(bot)}
    assert _registered_commands(bot) - menu == MENU_EXEMPT


def test_menu_contains_no_privileged_command(bot):
    menu = {c.command for c in _menu_commands(bot)}
    assert menu & MENU_EXEMPT == set()


def test_listsignal_is_in_the_menu(bot):
    assert "listsignal" in {c.command for c in _menu_commands(bot)}


# --- edited messages are processed like new ones (Telegram Web reissue) ---

def _real_update(bot, text, *, edited: bool, command: bool = True):
    """A genuine telegram.Update (not a SimpleNamespace) so the handlers'
    own check_update runs — that is the code under test here."""
    import datetime as dt
    from telegram import Update, Message, Chat, User, MessageEntity

    ents = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
    m = Message(message_id=1, date=dt.datetime.now(dt.timezone.utc),
                chat=Chat(id=1, type="private"),
                from_user=User(id=1, first_name="a", is_bot=False),
                text=text, entities=ents if command else [])
    # CommandHandler reads bot.username to resolve `/cmd@name`; the real ExtBot
    # refuses that uninitialised, and initialising it would mean network.
    m.set_bot(SimpleNamespace(username="lortebot"))
    return Update(update_id=1, **({"edited_message": m} if edited else {"message": m}))


def _handlers(bot):
    return [h for group in bot._app.handlers.values() for h in group]


def _fires(bot, update, kind):
    from telegram.ext import CommandHandler, MessageHandler
    want = CommandHandler if kind == "command" else MessageHandler
    return [h for h in _handlers(bot)
            if isinstance(h, want) and h.check_update(update)]


def test_edited_command_re_fires(bot):
    # Telegram Web reissues a command by editing the previous bubble, which
    # arrives as an `edited_message` (PTB resolves `effective_message` to it).
    # The command must run, exactly like a new message — editing `/help` into
    # `/get` has to run `/get`, not be dropped.
    assert _fires(bot, _real_update(bot, "/get T", edited=False), "command")
    assert _fires(bot, _real_update(bot, "/get T", edited=True), "command")


def test_edited_unknown_command_fires(bot):
    # Same for the unknown catch-all: an edited unknown command still replies.
    for edited in (False, True):
        assert _fires(bot, _real_update(bot, "/nosuch", edited=edited), "message")


def test_edited_plain_text_routes_to_arg_handler(bot):
    # A plain-text edit must reach `_on_arg_reply` too: on Web the argument to a
    # ForceReply prompt can arrive as an edit. The `_pending` window gates actual
    # consumption (see test_on_arg_reply_consumes_edited_argument).
    for edited in (False, True):
        assert _fires(bot, _real_update(bot, "SM1_T", edited=edited, command=False), "message")


# --- command trace (traceCmd) ---

def _trace_update(text, user_id, username="mario"):
    return SimpleNamespace(
        effective_message=SimpleNamespace(text=text),
        effective_user=SimpleNamespace(id=user_id, username=username),
        effective_chat=SimpleNamespace(id=user_id),
    )


def test_trace_off_returns_handler_untouched(bot):
    # trace_cmd defaults off in the fixture -> zero wrapping, zero overhead.
    async def cb(u, c):
        pass
    assert bot._traced(cb) is cb


def test_trace_logs_in_and_out(bot, caplog):
    bot._cfg.trace_cmd = True
    ran = []

    async def cb(u, c):
        ran.append(1)

    wrapped = bot._traced(cb)
    assert wrapped is not cb                       # now actually wrapped
    with caplog.at_level(logging.INFO, logger="bot.cmdtrace"):
        asyncio.run(wrapped(_trace_update("/get T", 7), None))

    assert ran == [1]                              # the real handler still ran
    msgs = [r.message for r in caplog.records if r.name == "bot.cmdtrace"]
    assert any(m.startswith("→") and "/get T" in m and "@mario" in m for m in msgs)
    assert any(m.startswith("←") and "ok" in m for m in msgs)


def test_trace_logs_failure_and_reraises(bot, caplog):
    bot._cfg.trace_cmd = True

    async def cb(u, c):
        raise KeyError("boom")

    wrapped = bot._traced(cb)
    with caplog.at_level(logging.INFO, logger="bot.cmdtrace"):
        with pytest.raises(KeyError):              # the exception must propagate
            asyncio.run(wrapped(_trace_update("/get T", 7), None))

    msgs = [r.message for r in caplog.records if r.name == "bot.cmdtrace"]
    assert any("FAILED" in m and "KeyError" in m for m in msgs)
    assert not any("ok" in m for m in msgs)        # a failure is not an ok


def test_trace_labels_unknown_command(bot, caplog):
    # The catch-all is wrapped with ok_label="unknown", so a non-existent
    # command reads as `unknown`, not `ok` — an unknown command completes
    # cleanly (it replies "Unknown command"), so without the label it would be
    # indistinguishable from a real command that ran.
    bot._cfg.trace_cmd = True

    async def cb(u, c):
        pass

    wrapped = bot._traced(cb, ok_label="unknown")
    with caplog.at_level(logging.INFO, logger="bot.cmdtrace"):
        asyncio.run(wrapped(_trace_update("/hhh", 7), None))

    msgs = [r.message for r in caplog.records if r.name == "bot.cmdtrace"]
    assert any(m.startswith("←") and "/hhh" in m and "unknown" in m for m in msgs)
    assert not any(m.startswith("←") and " ok " in m for m in msgs)


def test_trace_marks_no_result_via_helper(bot, caplog):
    # `_reply_no_match` sets the outcome; `_traced` reads it back. End-to-end
    # through the wrapper so the ContextVar stays in one task (its set inside a
    # separately-run coroutine would land in a copied context and not propagate).
    bot._cfg.trace_cmd = True
    bot._app = _fake_app([])

    async def cb(u, c):
        await bot._reply_no_match(7)

    with caplog.at_level(logging.INFO, logger="bot.cmdtrace"):
        asyncio.run(bot._traced(cb)(_trace_update("/get dddd", 7), None))

    msgs = [r.message for r in caplog.records if r.name == "bot.cmdtrace"]
    assert any(m.startswith("←") and "no-result" in m for m in msgs)
    assert not any(m.startswith("←") and " ok " in m for m in msgs)


def test_trace_marks_bad_input_via_helper(bot, caplog):
    bot._cfg.trace_cmd = True
    sent = []
    bot._app = _fake_app(sent)

    async def cb(u, c):
        await bot._reply_bad_input(7, "Usage: /setAlarm <sensor> <value>")

    with caplog.at_level(logging.INFO, logger="bot.cmdtrace"):
        asyncio.run(bot._traced(cb)(_trace_update("/setAlarm x", 7), None))

    msgs = [r.message for r in caplog.records if r.name == "bot.cmdtrace"]
    assert any(m.startswith("←") and "bad-input" in m for m in msgs)
    assert sent and sent[-1][1].startswith("Usage")   # the reply still went out


def test_trace_marks_denied_via_helper(bot, caplog):
    # A permission denial ("Not authorized.") is `denied`, not `ok` — the
    # request was well-formed, the caller just lacked the right.
    bot._cfg.trace_cmd = True
    sent = []
    bot._app = _fake_app(sent)

    async def cb(u, c):
        await bot._reply_denied(7)

    with caplog.at_level(logging.INFO, logger="bot.cmdtrace"):
        asyncio.run(bot._traced(cb)(_trace_update("/setAlarm SM1_T 30", 7), None))

    msgs = [r.message for r in caplog.records if r.name == "bot.cmdtrace"]
    assert any(m.startswith("←") and "denied" in m for m in msgs)
    assert sent and sent[-1][1] == "Not authorized."
