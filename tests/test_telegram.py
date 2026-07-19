"""Tests for bot.telegram_bot pure helpers — sensor/blackout resolution with
visibility gating, sort flags, the registration-token HMAC, digest building,
and the small formatting helpers. No network: the PTB Application builds
offline and we never start polling.
"""
import asyncio
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
    async def send_message(chat_id, text, **kw):
        sent.append((chat_id, text))

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


# /getAlarm

def test_getalarm_named_shows_band(hbot, temp_db):
    temp_db.set_threshold("SM1_T", 30.0)
    temp_db.set_threshold_low("SM1_T", 10.0)
    sent = _run(hbot, hbot._cmd_getalarm, ADMIN, "SM1_T")
    assert "SM1_T" in sent[-1][1] and "10" in sent[-1][1] and "30" in sent[-1][1]


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


def test_last5alarm_named(hbot, temp_db):
    temp_db.insert_alarm("SM1_T", "ALARM", "SM1_T: hot")
    sent = _run(hbot, hbot._cmd_last5alarm, ADMIN, "SM1_T")
    assert "hot" in sent[-1][1]


def test_last5alarm_unknown_sensor(hbot, temp_db):
    sent = _run(hbot, hbot._cmd_last5alarm, ADMIN, "NOPE")
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
    return SimpleNamespace(
        message=SimpleNamespace(text=text, reply_to_message=None),
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
