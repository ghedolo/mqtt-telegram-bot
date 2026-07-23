import json
import logging
import asyncio
import ssl
from typing import Callable, Awaitable, Optional

import paho.mqtt.client as mqtt

from .config import AppConfig

log = logging.getLogger(__name__)

_MAX_PAYLOAD_BYTES = 64 * 1024


def _log_future_exc(fut):
    exc = fut.exception()
    if exc is not None:
        log.error("Async handler failed: %s", exc)


def _coerce(sc, raw):
    """Parse a payload node to a number. A bool/number goes through float()
    directly; a discrete string payload (e.g. "dim"/"bright") is mapped to its
    numeric code via the field's `states` map used in reverse (label → value).
    Re-raises the original error if neither applies, preserving the caller's
    absent-field vs unparseable distinction."""
    try:
        return float(raw)
    except (ValueError, TypeError):
        if sc.states:
            for num, label in sc.states.items():
                if label == raw:
                    return num
        raise


def _parse_availability(raw: bytes) -> Optional[bool]:
    """Parse a zigbee2mqtt availability payload to True (online)/False (offline).
    Handles both formats z2m emits: JSON `{"state":"online"}` and the legacy
    plain string `online`/`offline`. Returns None for anything unrecognised."""
    try:
        text = raw.decode().strip()
    except Exception:
        return None
    if text.startswith("{"):
        try:
            text = str(json.loads(text).get("state", "")).strip()
        except Exception:
            return None
    low = text.lower()
    if low == "online":
        return True
    if low == "offline":
        return False
    return None


class MqttClient:
    def __init__(
        self,
        cfg: AppConfig,
        on_reading: Callable[[str, float], Awaitable[None]],
        on_topic_message: Optional[Callable[[str], Awaitable[None]]] = None,
        on_availability: Optional[Callable[[str, bool], Awaitable[None]]] = None,
    ):
        self._cfg = cfg
        self._on_reading = on_reading
        self._on_topic_message = on_topic_message
        self._on_availability = on_availability
        # Signals share the dispatch path with sensors — both expose .name and
        # .json_path — but are subscribed here too so their topic is received.
        self._topic_map: dict[str, list] = {}
        for sc in (*cfg.sensors.values(), *cfg.signals.values()):
            self._topic_map.setdefault(sc.topic, []).append(sc)
        # zigbee2mqtt availability topics → device key. Kept separate from the
        # sensor topic map: these carry an online/offline state, not a reading.
        self._availability_map: dict[str, str] = {
            d.availability_topic: d.key
            for d in cfg.devices.values()
            if d.availability_topic
        }
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if cfg.mqtt_username:
            self._client.username_pw_set(cfg.mqtt_username, cfg.mqtt_password)
        if cfg.mqtt_tls:
            self._client.tls_set(cert_reqs=ssl.CERT_NONE)
            self._client.tls_insecure_set(True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("MQTT connected")
            for topic in (*self._topic_map, *self._availability_map):
                client.subscribe(topic)
                log.info("Subscribed to %s", topic)
        else:
            log.error("MQTT connect failed: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        device_key = self._availability_map.get(msg.topic)
        if device_key is not None:
            # An availability topic: it carries a device online/offline state,
            # not a reading. Route it out here (never through _on_topic_message,
            # so it can't feed the data-cadence offline heuristic) and stop.
            online = _parse_availability(msg.payload)
            if online is not None and self._loop and self._on_availability:
                asyncio.run_coroutine_threadsafe(
                    self._on_availability(device_key, online), self._loop
                ).add_done_callback(_log_future_exc)
            return

        sensors = self._topic_map.get(msg.topic)
        if not sensors:
            return

        if len(msg.payload) > _MAX_PAYLOAD_BYTES:
            log.warning(
                "Payload too large on %s (%d bytes), dropped",
                msg.topic, len(msg.payload),
            )
            return

        if self._loop and self._on_topic_message:
            asyncio.run_coroutine_threadsafe(
                self._on_topic_message(msg.topic), self._loop
            ).add_done_callback(_log_future_exc)

        try:
            payload = msg.payload.decode()
        except Exception:
            log.warning("Cannot decode payload for topic %s", msg.topic)
            return

        for sc in sensors:
            try:
                if sc.json_path:
                    data = json.loads(payload)
                    node = data
                    for key in sc.json_path.split("."):
                        node = node[key]
                    value = _coerce(sc, node)
                else:
                    value = _coerce(sc, payload)
            except (KeyError, TypeError):
                continue  # field absent from this message — normal for intermittent fields
            except Exception:
                log.warning("Cannot parse payload for %s: %r", sc.name, msg.payload)
                continue

            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._on_reading(sc.name, value), self._loop
                ).add_done_callback(_log_future_exc)

    def start(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._client.connect(self._cfg.mqtt_host, self._cfg.mqtt_port, keepalive=60)
        self._client.loop_start()

    def stop(self):
        self._client.loop_stop()
        self._client.disconnect()
