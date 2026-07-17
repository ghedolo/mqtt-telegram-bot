"""Reading ingestion path.

process_reading is the single MQTT-message → SQLite → alarm-check flow, kept
out of main.on_reading's closure so the whole path is unit-testable. The
value is rounded to the field's configured decimals *before* storage and
alarm evaluation, matching the precision of the alarm thresholds. Out-of-range
readings (outside validMin/validMax) are still stored, but skip alarm checks
so a sensor glitch never raises an alarm.
"""
import logging

from . import db

log = logging.getLogger(__name__)


async def process_reading(cfg, alarms, sensor: str, value: float):
    if cfg.is_signal(sensor):
        # A Signal is never stored and has no thresholds: keep only the latest
        # value in memory and re-evaluate the blackout it feeds.
        alarms.record_signal(sensor, value)
        await alarms.check_blackout_for(sensor)
        return
    value = round(value, cfg.decimals_of(sensor))
    log.info("Reading: %s = %s", sensor, cfg.fmt(sensor, value))
    db.insert_reading(sensor, value)
    if not cfg.is_valid(sensor, value):
        log.info("Out-of-range reading ignored for alarms: %s = %s",
                 sensor, cfg.fmt(sensor, value))
        return
    await alarms.check_threshold(sensor, value)
    await alarms.check_threshold_low(sensor, value)
    await alarms.check_blackout_for(sensor)
