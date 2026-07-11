import asyncio
import logging
import signal

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
    cfg = load("sensors.d", "credentials.yaml")
    level = _DEBUG_LEVELS.get(cfg.debug, logging.INFO)
    logging.getLogger().setLevel(level)
    db.init()

    for sc in cfg.sensors.values():
        if sc.default_alarm_high is not None and db.get_threshold(sc.name) is None:
            db.set_threshold(sc.name, sc.default_alarm_high)
            log.info("High threshold for %s set from config: %s", sc.name, sc.default_alarm_high)
        if sc.default_alarm_low is not None and db.get_threshold_low(sc.name) is None:
            db.set_threshold_low(sc.name, sc.default_alarm_low)
            log.info("Low threshold for %s set from config: %s", sc.name, sc.default_alarm_low)

    tg = TelegramBot(cfg, reload_fn=lambda: load("sensors.d", "credentials.yaml"))

    async def notify(sensor: str, text: str):
        try:
            await tg.notify_sensor(sensor, text)
        except Exception:
            log.exception("Failed to send alarm notification for %s", sensor)

    async def notify_device(device_key: str, text: str):
        try:
            await tg.notify_device(device_key, text)
        except Exception:
            log.exception("Failed to send device alarm for %s", device_key)

    async def notify_blackout(group_id: str, text: str):
        try:
            await tg.notify_blackout(group_id, text)
        except Exception:
            log.exception("Failed to send blackout alarm for %s", group_id)

    alarms = AlarmManager(
        threshold_repeat=cfg.alarm_threshold_repeat,
        offline_repeat=cfg.alarm_offline_repeat,
        notify_fn=notify,
        notify_device_fn=notify_device,
        fmt_fn=cfg.fmt,
        notify_blackout_fn=notify_blackout,
        blackout_groups=cfg.blackouts,
    )

    tg.last_mqtt_fn = alarms.last_mqtt_ts
    tg.reset_alarm_fn = alarms.reset_sensor_alarm
    tg.apply_alarm_config_fn = lambda new: alarms.apply_config(
        new.alarm_threshold_repeat, new.alarm_offline_repeat, new.blackouts
    )

    async def on_reading(sensor: str, value: float):
        value = round(value, cfg.decimals_of(sensor))
        log.info("Reading: %s = %s", sensor, cfg.fmt(sensor, value))
        db.insert_reading(sensor, value)
        if not cfg.is_valid(sensor, value):
            log.info("Out-of-range reading ignored for alarms: %s = %s", sensor, cfg.fmt(sensor, value))
            return
        await alarms.check_threshold(sensor, value)
        await alarms.check_threshold_low(sensor, value)
        await alarms.check_blackout_for(sensor)

    async def on_topic_message(topic: str):
        alarms.record_topic_message(topic)

    loop = asyncio.get_running_loop()
    mqtt = MqttClient(cfg, on_reading, on_topic_message=on_topic_message)
    mqtt.start(loop)

    await tg.run()
    if not cfg.silent_start:
        await tg.send("🐶 LorTe is alive & sniffing! You can always say /help")

    # periodic tasks
    async def archive_loop():
        # Fire at a fixed wall-clock time (not a relative sleep): the host is
        # powered off overnight, so a plain sleep(86400) never elapses on ~9h
        # of daily uptime and archiving would never run. Pick archive_time
        # within host-on hours. Same next-target scheme as digest_loop, so a
        # restart just waits for the next occurrence instead of resetting.
        from datetime import datetime, timedelta
        h, m = (int(x) for x in cfg.archive_time.split(":"))
        while True:
            now = datetime.now()
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            try:
                db.archive_old_readings(cfg.retention_days)
            except Exception:
                log.exception("archive_old_readings failed")

    async def digest_loop():
        from datetime import datetime, timedelta
        h, m = (int(x) for x in cfg.digest_time.split(":"))
        while True:
            now = datetime.now()
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            for chat_id in db.get_all_dm_registered():
                try:
                    text = tg.build_digest(chat_id)
                    if text:
                        await tg.send_dm_to(chat_id, text, silent=True, parse_mode="Markdown")
                except Exception:
                    log.exception("Failed to send daily digest to %s", chat_id)

    tasks = [
        asyncio.create_task(alarms.run_offline_checks(cfg.devices)),
        asyncio.create_task(archive_loop()),
        asyncio.create_task(digest_loop()),
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
