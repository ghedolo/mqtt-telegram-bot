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
    default_alarm: Optional[float]
    viewers: list[str] = field(default_factory=list)
    admins: list[str] = field(default_factory=list)


@dataclass
class AppConfig:
    telegram_token: str
    telegram_group_id: int
    groups: dict[str, list[int]]
    superadmin: list[int]
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
    debug: int
    digest_time: str

    def _members(self, group_names: list[str]) -> set[int]:
        result: set[int] = set()
        for g in group_names:
            result.update(self.groups.get(g, []))
        return result

    def viewers_of(self, sensor: str) -> set[int]:
        sc = self.sensors.get(sensor)
        if sc is None:
            return set()
        return self._members(sc.viewers) | self._members(sc.admins)

    def admins_of(self, sensor: str) -> set[int]:
        sc = self.sensors.get(sensor)
        if sc is None:
            return set()
        return self._members(sc.admins)

    def is_viewer(self, user_id: int, sensor: str) -> bool:
        return user_id in self.viewers_of(sensor)

    def is_admin(self, user_id: int, sensor: str) -> bool:
        return user_id in self.admins_of(sensor)

    def is_any_admin(self, user_id: int) -> bool:
        return any(user_id in self.admins_of(s) for s in self.sensors)

    def is_superadmin(self, user_id: int) -> bool:
        return user_id in self.superadmin

    def visible_sensors(self, user_id: int) -> list[str]:
        return [n for n in self.sensors if self.is_viewer(user_id, n)]


def _load_yaml(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load(
    public: str = "sensors.yaml",
    secret: str = "credentials.yaml",
) -> AppConfig:
    raw = _load_yaml(public)
    sec = _load_yaml(secret)

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
            default_alarm=float(sc["defaultAlarm"]) if "defaultAlarm" in sc else None,
            viewers=list(sc.get("viewers", [])),
            admins=list(sc.get("admins", [])),
        )

    tg = sec["telegram"]
    mq = sec["mqtt"]
    raw_groups = sec.get("groups", {})
    groups = {g: [int(i) for i in members] for g, members in raw_groups.items()}
    superadmin = [int(i) for i in sec.get("superadmin", [])]

    return AppConfig(
        telegram_token=tg["token"],
        telegram_group_id=int(tg["group_id"]),
        groups=groups,
        superadmin=superadmin,
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
        debug=int(defaults.get("debug", 1)),
        digest_time=str(tg.get("digest_time", "15:00")),
    )
