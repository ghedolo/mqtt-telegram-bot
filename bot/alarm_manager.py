import asyncio
import time
import logging
from dataclasses import dataclass, field
from typing import Callable, Awaitable

from . import db
from .config import DeviceConfig

log = logging.getLogger(__name__)


@dataclass
class AlarmState:
    sensor: str
    kind: str
    active: bool = False
    last_notified: int = 0


class AlarmManager:
    def __init__(
        self,
        threshold_repeat: int,
        offline_repeat: int,
        notify_fn: Callable[[str, str], Awaitable[None]],
        notify_device_fn: Callable[[str, str], Awaitable[None]],
    ):
        self._threshold_repeat = threshold_repeat
        self._offline_repeat = offline_repeat
        self._notify = notify_fn
        self._notify_device = notify_device_fn
        self._states: dict[str, AlarmState] = {}
        self._started_at = int(time.time())
        self._last_topic_ts: dict[str, int] = {}

    def record_topic_message(self, topic: str):
        self._last_topic_ts[topic] = int(time.time())

    def last_mqtt_ts(self) -> int | None:
        return max(self._last_topic_ts.values(), default=None)

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
                msg = f"ALARM {sensor}: {value:.1f} > thr {threshold:.1f}"
                db.insert_alarm(sensor, "ALARM", msg)
                await self._notify(sensor, msg)
            elif (now - state.last_notified) >= self._threshold_repeat:
                state.last_notified = now
                msg = f"ALARM {sensor}: {value:.1f} > thr {threshold:.1f}"
                db.insert_alarm(sensor, "ALARM", msg)
                await self._notify(sensor, msg)
        else:
            if state.active:
                state.active = False
                msg = f"OK {sensor}: {value:.1f} < thr {threshold:.1f}"
                db.insert_alarm(sensor, "OK", msg)
                await self._notify(sensor, msg)

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
                msg = f"ALARM {sensor}: {value:.1f} < thr_low {threshold:.1f}"
                db.insert_alarm(sensor, "ALARM_LOW", msg)
                await self._notify(sensor, msg)
            elif (now - state.last_notified) >= self._threshold_repeat:
                state.last_notified = now
                msg = f"ALARM {sensor}: {value:.1f} < thr_low {threshold:.1f}"
                db.insert_alarm(sensor, "ALARM_LOW", msg)
                await self._notify(sensor, msg)
        else:
            if state.active:
                state.active = False
                msg = f"OK {sensor}: {value:.1f} > thr_low {threshold:.1f}"
                db.insert_alarm(sensor, "OK_LOW", msg)
                await self._notify(sensor, msg)

    def _device_last_ts(self, device: DeviceConfig) -> int:
        """Most recent message timestamp across all topics of a device."""
        if device.topic:
            return self._last_topic_ts.get(device.topic, 0)
        tss = [self._last_topic_ts.get(sc.topic, 0) for sc in device.fields.values()]
        return max(tss) if tss else 0

    async def check_offline(self, device: DeviceConfig):
        if db.is_silenced(device.key):
            return

        offline_after = device.interval * 3
        if (int(time.time()) - self._started_at) < offline_after:
            return

        state = self._state(device.key, "offline")
        now = int(time.time())

        last_ts = self._device_last_ts(device)
        if last_ts == 0:
            # no in-memory record yet — fall back to DB
            for sc in device.fields.values():
                row = db.get_latest(sc.name)
                if row and row["ts"] > last_ts:
                    last_ts = row["ts"]

        if last_ts == 0 or (now - last_ts) > offline_after:
            if not state.active:
                state.active = True
                state.last_notified = now
                msg = f"OFFLINE {device.key}: no data for >{offline_after}s"
                db.insert_alarm(device.key, "OFFLINE", msg)
                await self._notify_device(device.key, msg)
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

    async def run_offline_checks(self, devices: dict):
        while True:
            for dev_key, device in list(devices.items()):
                try:
                    await self.check_offline(device)
                except Exception:
                    log.exception("Error checking offline for %s", dev_key)
            await asyncio.sleep(60)
