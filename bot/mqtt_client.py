import json
import logging
import asyncio
import ssl
from typing import Callable, Awaitable

import paho.mqtt.client as mqtt

from .config import AppConfig, SensorConfig

log = logging.getLogger(__name__)


class MqttClient:
    def __init__(
        self,
        cfg: AppConfig,
        on_reading: Callable[[str, float], Awaitable[None]],
    ):
        self._cfg = cfg
        self._on_reading = on_reading
        self._topic_map: dict[str, SensorConfig] = {
            sc.topic: sc for sc in cfg.sensors.values()
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
            for topic in self._topic_map:
                client.subscribe(topic)
                log.info("Subscribed to %s", topic)
        else:
            log.error("MQTT connect failed: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        sc = self._topic_map.get(msg.topic)
        if sc is None:
            return
        try:
            payload = msg.payload.decode()
            if sc.json_field:
                data = json.loads(payload)
                value = float(data[sc.json_field])
            else:
                value = float(payload)
        except Exception:
            log.warning("Cannot parse payload for %s: %r", sc.name, msg.payload)
            return

        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._on_reading(sc.name, value), self._loop
            )

    def start(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._client.connect(self._cfg.mqtt_host, self._cfg.mqtt_port, keepalive=60)
        self._client.loop_start()

    def stop(self):
        self._client.loop_stop()
        self._client.disconnect()
