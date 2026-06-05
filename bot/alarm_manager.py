import asyncio
import time
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

from . import db

log = logging.getLogger(__name__)


@dataclass
class AlarmState:
    sensor: str
    kind: str          # "threshold" or "offline"
    active: bool = False
    last_notified: int = 0


class AlarmManager:
    def __init__(
        self,
        threshold_repeat: int,
        offline_repeat: int,
        notify_fn: Callable[[str], Awaitable[None]],
    ):
        self._threshold_repeat = threshold_repeat
        self._offline_repeat = offline_repeat
        self._notify = notify_fn
        self._states: dict[str, AlarmState] = {}
        self._started_at = int(time.time())

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
            repeat = self._threshold_repeat
            if not state.active:
                state.active = True
                state.last_notified = now
                msg = f"ALARM {sensor}: temperature {value:.1f} above threshold {threshold:.1f}"
                db.insert_alarm(sensor, "ALARM", msg)
                await self._notify(msg)
            elif (now - state.last_notified) >= repeat:
                state.last_notified = now
                msg = f"ALARM {sensor}: still {value:.1f} (threshold {threshold:.1f})"
                db.insert_alarm(sensor, "ALARM", msg)
                await self._notify(msg)
        else:
            if state.active:
                state.active = False
                msg = f"OK {sensor}: temperature {value:.1f} back below threshold {threshold:.1f}"
                db.insert_alarm(sensor, "OK", msg)
                await self._notify(msg)

    async def check_offline(self, sensor: str, interval: int):
        if db.is_silenced(sensor):
            return

        offline_after = interval * 3
        if (int(time.time()) - self._started_at) < offline_after:
            return

        state = self._state(sensor, "offline")
        now = int(time.time())
        latest = db.get_latest(sensor)

        if latest is None or (now - latest["ts"]) > offline_after:
            repeat = self._offline_repeat
            if not state.active:
                state.active = True
                state.last_notified = now
                msg = f"OFFLINE {sensor}: no data received for >{offline_after}s"
                db.insert_alarm(sensor, "OFFLINE", msg)
                await self._notify(msg)
            elif (now - state.last_notified) >= repeat:
                state.last_notified = now
                msg = f"OFFLINE {sensor}: still no data"
                db.insert_alarm(sensor, "OFFLINE", msg)
                await self._notify(msg)
        else:
            if state.active:
                state.active = False
                db.unsilence_sensor(sensor)
                msg = f"ONLINE {sensor}: sensor back online"
                db.insert_alarm(sensor, "ONLINE", msg)
                await self._notify(msg)

    async def run_offline_checks(self, sensors: dict):
        while True:
            for name, sc in sensors.items():
                try:
                    await self.check_offline(name, sc.interval)
                except Exception:
                    log.exception("Error checking offline for %s", name)
            await asyncio.sleep(60)
