import yaml
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SensorConfig:
    name: str
    topic: str
    json_field: Optional[str]
    interval: int
    info: str
    unit: str


@dataclass
class AppConfig:
    telegram_token: str
    telegram_group_id: int
    admin_ids: list[int]
    poll_interval: int
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str
    mqtt_tls: bool
    sensors: dict[str, SensorConfig]
    retention_days: int
    alarm_threshold_repeat: int
    alarm_offline_repeat: int


def load(path: str = "config.yaml") -> AppConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)

    defaults = raw.get("defaults", {})
    default_interval = defaults.get("interval", 300)

    sensors = {}
    for name, sc in raw["sensors"].items():
        sensors[name] = SensorConfig(
            name=name,
            topic=sc["topic"],
            json_field=sc.get("json_field"),
            interval=sc.get("interval", default_interval),
            info=sc.get("info", "")[:25],
            unit=sc.get("unit", ""),
        )

    tg = raw["telegram"]
    mq = raw["mqtt"]

    return AppConfig(
        telegram_token=tg["token"],
        telegram_group_id=int(tg["group_id"]),
        admin_ids=[int(i) for i in tg.get("admin_ids", [])],
        poll_interval=max(1, min(10, int(tg.get("poll_interval", 3)))),
        mqtt_host=mq["host"],
        mqtt_port=int(mq.get("port", 1883)),
        mqtt_username=mq.get("username", ""),
        mqtt_password=mq.get("password", ""),
        mqtt_tls=bool(mq.get("tls", int(mq.get("port", 1883)) == 8883)),
        sensors=sensors,
        retention_days=int(defaults.get("retention_days", 30)),
        alarm_threshold_repeat=int(defaults.get("alarm_threshold_repeat", 720)),
        alarm_offline_repeat=int(defaults.get("alarm_offline_repeat", 3600)),
    )
