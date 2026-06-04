import asyncio
import logging
import signal
import sys

from .config import load
from . import db
from .mqtt_client import MqttClient
from .alarm_manager import AlarmManager
from .telegram_bot import TelegramBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

_DEBUG_LEVELS = {
    0: logging.CRITICAL,
    1: logging.INFO,
    2: logging.DEBUG,
}


async def main():
    cfg = load("sensors.yaml", "credentials.yaml")
    level = _DEBUG_LEVELS.get(cfg.debug, logging.INFO)
    logging.getLogger().setLevel(level)
    db.init()

    for sc in cfg.sensors.values():
        if sc.alarm is not None and db.get_threshold(sc.name) is None:
            db.set_threshold(sc.name, sc.alarm)
            log.info("Threshold for %s set from config: %s", sc.name, sc.alarm)

    tg = TelegramBot(cfg)

    async def notify(text: str):
        try:
            await tg.send(text)
        except Exception:
            log.exception("Failed to send Telegram message")

    alarms = AlarmManager(
        threshold_repeat=cfg.alarm_threshold_repeat,
        offline_repeat=cfg.alarm_offline_repeat,
        notify_fn=notify,
    )

    async def on_reading(sensor: str, value: float):
        log.info("Reading: %s = %.2f", sensor, value)
        db.insert_reading(sensor, value)
        await alarms.check_threshold(sensor, value)

    loop = asyncio.get_running_loop()
    mqtt = MqttClient(cfg, on_reading)
    mqtt.start(loop)

    await tg.run()
    await notify("🐶 LorTe is alive & sniffing! You can always say /help")

    # periodic tasks
    async def purge_loop():
        while True:
            await asyncio.sleep(3600)
            db.purge_old_readings(cfg.retention_days)

    tasks = [
        asyncio.create_task(alarms.run_offline_checks(cfg.sensors)),
        asyncio.create_task(purge_loop()),
    ]

    stop_event = asyncio.Event()

    def _shutdown(sig):
        log.info("Received %s, shutting down", sig.name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    await stop_event.wait()

    mqtt.stop()
    await tg.stop()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("Shutdown complete")


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
