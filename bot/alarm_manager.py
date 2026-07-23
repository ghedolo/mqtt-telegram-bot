import asyncio
import time
import logging
from dataclasses import dataclass
from typing import Callable, Awaitable

from . import db
from .config import DeviceConfig

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
    ):
        self._threshold_repeat = threshold_repeat
        self._offline_repeat = offline_repeat
        self._notify = notify_fn
        self._notify_device = notify_device_fn
        self._notify_blackout = notify_blackout_fn
        self._blackout_groups = blackout_groups or {}
        self._fmt = fmt_fn
        self._states: dict[str, AlarmState] = {}
        self._started_at = int(time.time())
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

    def last_mqtt_ts(self) -> int | None:
        return max(self._last_topic_ts.values(), default=None)

    def reset_sensor_alarm(self, sensor: str):
        for kind in ("threshold", "threshold_low"):
            k = self._key(sensor, kind)
            if k in self._states:
                self._states[k].active = False

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

    async def check_offline(self, device: DeviceConfig):
        now = int(time.time())

        if device.availability_topic and device.key in self._availability:
            # Trust zigbee2mqtt: it already knows a battery sensor going quiet for
            # hours is normal, so its online/offline is authoritative here and the
            # data-cadence heuristic is skipped entirely.
            if (now - self._started_at) < AVAIL_GRACE:
                return
            offline = not self._availability[device.key]
            first_msg = f"OFFLINE {device.key}: unreachable (zigbee2mqtt)"
        else:
            offline_after = device.interval * 3
            if (now - self._started_at) < offline_after:
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

        all_dark = True
        any_lit = False
        for name in group.fields:
            # Signal-backed fields live in the in-memory cache (never in the DB);
            # a regular field is never in the cache, so this routes correctly.
            row = self._signal_latest.get(name) or db.get_latest(name)
            fresh = row is not None and (now - row["ts"]) <= group.stale_after
            if not fresh:
                all_dark = False            # UNKNOWN
            elif row["value"] >= group.below:
                all_dark = False
                any_lit = True              # LIT
            # else: DARK

        if all_dark:
            if state.since == 0:
                state.since = now
            sustained = (now - state.since) >= group.for_seconds
            if sustained and not state.active:
                state.active = True
                state.last_notified = now
                msg = f"⚡ BLACKOUT {group.info}: no current for >{group.for_seconds}s"
                db.insert_alarm(group.id, "BLACKOUT", msg)
                await self._notify_blackout(group.id, msg)
            elif state.active and (now - state.last_notified) >= group.repeat_seconds:
                state.last_notified = now
                msg = f"⚡ BLACKOUT {group.info}: still no current"
                db.insert_alarm(group.id, "BLACKOUT", msg)
                await self._notify_blackout(group.id, msg)
        elif any_lit:
            # confirmed power on at least one field → real end
            state.since = 0
            if state.active:
                state.active = False
                msg = f"🔌 BLACKOUT END {group.info}: power restored"
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
            for dev_key, device in list(devices.items()):
                try:
                    await self.check_offline(device)
                except Exception:
                    log.exception("Error checking offline for %s", dev_key)
            await asyncio.sleep(60)
