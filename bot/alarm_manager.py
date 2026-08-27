import asyncio
import time
import logging
from dataclasses import dataclass
from typing import Callable, Awaitable, Optional

from . import db
from .config import DeviceConfig
from .fmt import fmt_duration

log = logging.getLogger(__name__)

# Startup grace for the availability path. z2m publishes availability as retained
# messages, so on connect the bot immediately learns each device's real state —
# without a grace, every restart would re-announce OFFLINE for devices z2m already
# knows are down. Short, since availability (unlike the data-cadence heuristic)
# needs no warm-up window to fill.
AVAIL_GRACE = 120


@dataclass
class AlarmState:
    sensor: str
    kind: str
    active: bool = False
    last_notified: int = 0
    since: int = 0          # blackout: when the all-dark condition first held (0 = not)


class AlarmManager:
    def __init__(
        self,
        threshold_repeat: int,
        offline_repeat: int,
        notify_fn: Callable[[str, str], Awaitable[None]],
        notify_device_fn: Callable[[str, str], Awaitable[None]],
        fmt_fn: Callable[[str, float], str],
        notify_blackout_fn: Callable[[str, str], Awaitable[None]] = None,
        blackout_groups: dict = None,
        is_valid_fn: Callable[[str, float], bool] = None,
        device_of_fn: Callable[[str], str] = None,
    ):
        self._threshold_repeat = threshold_repeat
        self._offline_repeat = offline_repeat
        self._notify = notify_fn
        self._notify_device = notify_device_fn
        self._notify_blackout = notify_blackout_fn
        self._blackout_groups = blackout_groups or {}
        self._fmt = fmt_fn
        # glitch filter, shared with the threshold path: a reading outside
        # validMin/validMax is stored but must not count as evidence
        self._is_valid = is_valid_fn or (lambda name, value: True)
        # sensor/signal name → configured device key, for naming who lost power
        self._device_of = device_of_fn or (lambda name: "")
        self._states: dict[str, AlarmState] = {}
        self._started_at = int(time.time())
        # device_key → when its offline grace started (process start for the
        # devices present at boot, first sight for one added by /reloadConfig)
        self._device_first_seen: dict[str, int] = {}
        self._last_topic_ts: dict[str, int] = {}
        # device_key → last-known zigbee2mqtt availability (True=online). Fed live
        # by the MQTT availability callback; read by check_offline for devices
        # that declare an availability topic.
        self._availability: dict[str, bool] = {}
        # Latest value of each Signal (never stored in the DB). check_blackout
        # reads this in preference to db.get_latest for signal-backed fields.
        self._signal_latest: dict[str, dict] = {}

    def apply_config(self, threshold_repeat: int, offline_repeat: int, blackout_groups: dict):
        """Hot-apply reloadable alarm settings (from /reloadConfig) without a restart."""
        self._threshold_repeat = threshold_repeat
        self._offline_repeat = offline_repeat
        self._blackout_groups = blackout_groups or {}

    def restore_states(self, last_alarms: dict, device_keys, group_ids):
        """Rebuild the alarm states a restart would otherwise drop.

        The states live in memory only, so a restart used to make the bot forget
        every outage it had already reported. The consequence was silent and
        one-directional: a Device that came back while the process was down
        never got its ONLINE, because the `active` flag the recovery branch
        keys off no longer existed — the users' last word on it stayed OFFLINE
        forever. Same for a blackout that ended across a restart.

        The `alarms` table already holds the answer: the newest row per subject
        says whether it was last reported as broken or as recovered. Only the
        raising kinds are restored (OFFLINE, BLACKOUT); a subject whose last row
        is a recovery is already in the default state. Subjects the config no
        longer knows are skipped, so a removed Device cannot be resurrected.

        The blackout onset is deliberately NOT invented: `since` stays 0, and
        the end message then reports the outage without a duration rather than
        with a made-up one.
        """
        for subject, (kind, ts) in last_alarms.items():
            if kind == "OFFLINE" and subject in device_keys:
                state = self._state(subject, "offline")
            elif kind == "BLACKOUT" and subject in group_ids:
                state = self._state(subject, "blackout")
            else:
                continue
            state.active = True
            # keep the repeat cadence continuous across the restart instead of
            # re-notifying immediately
            state.last_notified = ts
            log.info("Restored %s alarm state for %s (last reported %s)",
                     state.kind, subject, kind)

    def record_topic_message(self, topic: str):
        self._last_topic_ts[topic] = int(time.time())

    def record_availability(self, device_key: str, online: bool):
        """Store a device's zigbee2mqtt availability (online/offline)."""
        self._availability[device_key] = online

    def record_signal(self, name: str, value: float):
        """Store a Signal's latest value in memory only (not the DB)."""
        self._signal_latest[name] = {"value": float(value), "ts": int(time.time())}

    def signal_snapshot(self) -> dict[str, dict]:
        """Read-only view of the in-memory Signal cache (for /listSignal)."""
        return dict(self._signal_latest)

    def availability_snapshot(self) -> dict[str, bool]:
        """Read-only view of the last zigbee2mqtt availability per device key
        (for /get, which marks a Sensor stale the same way check_offline does).
        Only devices that publish an availability topic ever appear here."""
        return dict(self._availability)

    def last_mqtt_ts(self) -> int | None:
        return max(self._last_topic_ts.values(), default=None)

    def reset_sensor_alarm(self, sensor: str, kind: Optional[str] = None):
        """Forget an active threshold alarm so the next crossing is treated as
        new. `kind` is "threshold" or "threshold_low"; None resets both.

        The caller must name the band it actually changed. Resetting both from a
        /setAlarm dropped a live low alarm's `active` flag, and the recovery
        branch keys off exactly that: the 🟢 never came, no OK_LOW row was
        written, and the alarm just evaporated."""
        kinds = (kind,) if kind else ("threshold", "threshold_low")
        for k in kinds:
            key = self._key(sensor, k)
            if key in self._states:
                self._states[key].active = False

    def _key(self, sensor: str, kind: str) -> str:
        return f"{sensor}:{kind}"

    def _state(self, sensor: str, kind: str) -> AlarmState:
        k = self._key(sensor, kind)
        if k not in self._states:
            self._states[k] = AlarmState(sensor=sensor, kind=kind)
        return self._states[k]

    async def check_threshold(self, sensor: str, value: float):
        threshold = db.get_threshold(sensor)
        if threshold is None:
            return

        state = self._state(sensor, "threshold")
        now = int(time.time())

        if value > threshold:
            if not state.active:
                state.active = True
                state.last_notified = now
                msg = f"{sensor}: {self._fmt(sensor, value)} > thr {self._fmt(sensor, threshold)}"
                db.insert_alarm(sensor, "ALARM", msg)
                await self._notify(sensor, f"🔴 {msg}")
            elif (now - state.last_notified) >= self._threshold_repeat:
                state.last_notified = now
                msg = f"{sensor}: {self._fmt(sensor, value)} > thr {self._fmt(sensor, threshold)}"
                db.insert_alarm(sensor, "ALARM", msg)
                await self._notify(sensor, f"🔴 {msg}")
        else:
            if state.active:
                state.active = False
                msg = f"{sensor}: {self._fmt(sensor, value)} < thr {self._fmt(sensor, threshold)}"
                db.insert_alarm(sensor, "OK", msg)
                await self._notify(sensor, f"🟢 {msg}")

    async def check_threshold_low(self, sensor: str, value: float):
        threshold = db.get_threshold_low(sensor)
        if threshold is None:
            return

        state = self._state(sensor, "threshold_low")
        now = int(time.time())

        if value < threshold:
            if not state.active:
                state.active = True
                state.last_notified = now
                msg = f"{sensor}: {self._fmt(sensor, value)} < thr_low {self._fmt(sensor, threshold)}"
                db.insert_alarm(sensor, "ALARM_LOW", msg)
                await self._notify(sensor, f"🔴 {msg}")
            elif (now - state.last_notified) >= self._threshold_repeat:
                state.last_notified = now
                msg = f"{sensor}: {self._fmt(sensor, value)} < thr_low {self._fmt(sensor, threshold)}"
                db.insert_alarm(sensor, "ALARM_LOW", msg)
                await self._notify(sensor, f"🔴 {msg}")
        else:
            if state.active:
                state.active = False
                msg = f"{sensor}: {self._fmt(sensor, value)} > thr_low {self._fmt(sensor, threshold)}"
                db.insert_alarm(sensor, "OK_LOW", msg)
                await self._notify(sensor, f"🟢 {msg}")

    def _device_last_ts(self, device: DeviceConfig) -> int:
        """Most recent message timestamp across all topics of a device."""
        if device.topic:
            return self._last_topic_ts.get(device.topic, 0)
        tss = [self._last_topic_ts.get(sc.topic, 0) for sc in device.fields.values()]
        return max(tss) if tss else 0

    def _grace_start(self, device_key: str) -> int:
        """When this device's startup grace began.

        Process start for everything present at boot; first sight for a device
        added by /reloadConfig, which the MQTT client has not subscribed to yet
        (subscriptions are wired at startup). Measuring its silence from process
        start declared it OFFLINE on the very next poll and kept repeating until
        someone restarted the bot — an alarm about the bot's own limitation.

        The stamps are laid down by run_offline_checks; a device checked outside
        that loop simply inherits the process-start grace."""
        return self._device_first_seen.get(device_key, self._started_at)

    async def check_offline(self, device: DeviceConfig):
        now = int(time.time())
        grace_start = self._grace_start(device.key)

        if device.availability_topic and device.key in self._availability:
            # Trust zigbee2mqtt: it already knows a battery sensor going quiet for
            # hours is normal, so its online/offline is authoritative here and the
            # data-cadence heuristic is skipped entirely.
            if (now - grace_start) < AVAIL_GRACE:
                return
            offline = not self._availability[device.key]
            first_msg = f"OFFLINE {device.key}: unreachable (zigbee2mqtt)"
        else:
            offline_after = device.interval * 3
            if (now - grace_start) < offline_after:
                return
            last_ts = self._device_last_ts(device)
            if last_ts == 0:
                # no in-memory record yet — fall back to DB
                for sc in device.fields.values():
                    row = db.get_latest(sc.name)
                    if row and row["ts"] > last_ts:
                        last_ts = row["ts"]
            offline = last_ts == 0 or (now - last_ts) > offline_after
            first_msg = f"OFFLINE {device.key}: no data for >{offline_after}s"

        state = self._state(device.key, "offline")

        if offline:
            if db.is_silenced(device.key):
                # ackOff active: suppress notifications, but keep tracking the
                # active state so a later reconnect still auto-clears the silence.
                state.active = True
                return
            if not state.active:
                state.active = True
                state.last_notified = now
                db.insert_alarm(device.key, "OFFLINE", first_msg)
                await self._notify_device(device.key, first_msg)
            elif (now - state.last_notified) >= self._offline_repeat:
                state.last_notified = now
                msg = f"OFFLINE {device.key}: still no data"
                db.insert_alarm(device.key, "OFFLINE", msg)
                await self._notify_device(device.key, msg)
        else:
            if state.active:
                state.active = False
                db.unsilence_sensor(device.key)
                msg = f"ONLINE {device.key}: back online"
                db.insert_alarm(device.key, "ONLINE", msg)
                await self._notify_device(device.key, msg)
            elif db.is_silenced(device.key):
                # Silenced while online (ackOff with no live outage): drop the
                # stale flag so it can't mute a future genuine offline forever.
                db.unsilence_sensor(device.key)

    def _classify_fields(self, group, now: int) -> list[tuple[str, str, Optional[float]]]:
        """Classify every watched field of a group from its latest reading.

        Returns (name, state, value) per field, state one of DARK / LIT / MISSING
        / STALE / INVALID. The last three are all UNKNOWN as far as the state
        machine is concerned — they are told apart only so the message can say
        *why* a field carried no evidence."""
        out = []
        for name in group.fields:
            # Signal-backed fields live in the in-memory cache (never in the DB);
            # a regular field is never in the cache, so this routes correctly.
            row = self._signal_latest.get(name) or db.get_latest(name)
            if row is None:
                out.append((name, "MISSING", None))
            elif (now - row["ts"]) > group.stale_after:
                out.append((name, "STALE", row["value"]))
            # An out-of-range sample is a glitch, not evidence: the threshold
            # path already refuses to act on one, and letting it stand here let
            # a single corrupted near-zero reading argue for a blackout (or a
            # wild spike argue for its end) long after the sample itself.
            elif not self._is_valid(name, row["value"]):
                out.append((name, "INVALID", row["value"]))
            elif row["value"] >= group.below:
                out.append((name, "LIT", row["value"]))
            else:
                out.append((name, "DARK", row["value"]))
        return out

    def _blackout_subject(self, group) -> str:
        """Who lost power: the device keys of the Fields the detection watches,
        read from the sensor config (`AppConfig.device_of`), deduped with order
        preserved — a group watching two currents of one meter names one subject.

        Falls back to splitting `{device}_{field}` only for a name the config
        does not know, which is the test/diagnostic case."""
        seen = []
        for name in group.fields:
            dev = self._device_of(name) or name.rsplit("_", 1)[0]
            if dev not in seen:
                seen.append(dev)
        return ", ".join(seen)

    def _fmt_blackout_fields(self, snapshot) -> str:
        """Render the per-field snapshot for a blackout message: the readings the
        decision was actually taken on, with the non-evidence ones named as such
        so a partial outage is legible from the message alone."""
        parts = []
        for name, st, value in snapshot:
            if st in ("DARK", "LIT"):
                parts.append(f"{name}={self._fmt(name, value)}")
            elif st == "STALE":
                parts.append(f"{name}=stale")
            elif st == "INVALID":
                parts.append(f"{name}=?")
            else:
                parts.append(f"{name}=n/a")
        return " ".join(parts)

    async def check_blackout(self, group):
        """Raise a blackout Alarm when every current Field in the group has a
        fresh reading below the threshold, sustained for the group duration.

        Each field is classified from its latest reading:
          - DARK    : fresh (age ≤ stale_after) and below the threshold
          - LIT     : fresh and at/above the threshold → power confirmed present
          - UNKNOWN : stale or missing → no evidence either way
        Raise when *all* fields are DARK. End (recovery) only on positive proof,
        i.e. when *any* field is LIT — a stale/UNKNOWN field never ends a
        blackout, so a meter dying mid-outage cannot emit a false recovery;
        that field's own device offline alarm covers the silence instead."""
        if self._notify_blackout is None:
            return
        now = int(time.time())
        state = self._state(group.id, "blackout")

        snapshot = self._classify_fields(group, now)
        all_dark = all(st == "DARK" for _, st, _ in snapshot)
        any_lit = any(st == "LIT" for _, st, _ in snapshot)
        fields = self._fmt_blackout_fields(snapshot)
        subject = self._blackout_subject(group)

        if all_dark:
            if state.since == 0:
                state.since = now
            sustained = (now - state.since) >= group.for_seconds
            if sustained and not state.active:
                state.active = True
                state.last_notified = now
                msg = f"⚡ BLACKOUT started. ({subject} outage). {fields}"
                db.insert_alarm(group.id, "BLACKOUT", msg)
                await self._notify_blackout(group.id, msg)
            elif state.active and (now - state.last_notified) >= group.repeat_seconds:
                state.last_notified = now
                msg = (f"⚡ BLACKOUT still no current after "
                       f"{fmt_duration(now - state.since)}. ({subject} outage). {fields}")
                db.insert_alarm(group.id, "BLACKOUT", msg)
                await self._notify_blackout(group.id, msg)
        elif any_lit:
            # confirmed power on at least one field → real end. Read the onset
            # before clearing it: it is what makes the END message report the
            # *whole* outage (sustain window and silent holds included), not
            # just the time since the alarm was raised.
            started = state.since
            state.since = 0
            if state.active:
                state.active = False
                # started == 0 only if the process restarted mid-outage and lost
                # the in-memory onset; report the end without inventing a length.
                for_how_long = f" after {fmt_duration(now - started)}" if started else ""
                msg = f"🔌 BLACKOUT end{for_how_long}. ({subject} restored). {fields}"
                db.insert_alarm(group.id, "BLACKOUT_END", msg)
                await self._notify_blackout(group.id, msg)
        # else: only UNKNOWN fields (stale) and none LIT → hold, no message

    async def check_blackout_for(self, sensor: str):
        """Event-driven blackout evaluation: re-check every group that watches
        this sensor, on each incoming reading (detection latency ≈ meter cadence)."""
        for group in self._blackout_groups.values():
            if sensor in group.fields:
                try:
                    await self.check_blackout(group)
                except Exception:
                    log.exception("Error checking blackout for %s", group.id)

    async def run_offline_checks(self, devices: dict):
        while True:
            # Stamp anything not seen before: on the first pass that is every
            # configured device, at ~process start; later it is exactly the
            # devices a /reloadConfig has just added, which then get a grace
            # window of their own instead of inheriting an already-expired one.
            now = int(time.time())
            for dev_key in devices:
                self._device_first_seen.setdefault(dev_key, now)
            for dev_key, device in list(devices.items()):
                try:
                    await self.check_offline(device)
                except Exception:
                    log.exception("Error checking offline for %s", dev_key)
            await asyncio.sleep(60)
