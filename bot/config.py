import yaml
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SensorConfig:
    name: str           # derived: {device_key}_{field_key}
    topic: str
    json_path: Optional[str]
    interval: int
    info: str           # from device
    unit: str
    default_alarm_high: Optional[float]
    default_alarm_low: Optional[float]
    valid_min: Optional[float] = None
    valid_max: Optional[float] = None
    viewers: list[str] = field(default_factory=list)
    admins: list[str] = field(default_factory=list)
    device_key: str = ""


@dataclass
class DeviceConfig:
    key: str
    topic: Optional[str]        # shared topic; None = per-field topics
    interval: int
    info: str
    note: str
    fields: dict[str, "SensorConfig"]   # field_key → SensorConfig


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
    sensors: dict[str, SensorConfig]    # sensor_name → SensorConfig (flat view)
    devices: dict[str, DeviceConfig]    # device_key → DeviceConfig
    retention_days: int
    alarm_threshold_repeat: int
    alarm_offline_repeat: int
    debug: int
    silent_start: bool
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

    def is_valid(self, sensor: str, value: float) -> bool:
        """True if value is within the sensor's plausible range (raw glitch filter).
        Range bounds are optional; an absent bound is not enforced."""
        sc = self.sensors.get(sensor)
        if sc is None:
            return True
        if sc.valid_min is not None and value < sc.valid_min:
            return False
        if sc.valid_max is not None and value > sc.valid_max:
            return False
        return True

    def visible_sensors(self, user_id: int) -> list[str]:
        return [n for n in self.sensors if self.is_viewer(user_id, n)]

    def resolve_sensor(self, name: str) -> str:
        """Map a user-supplied sensor name to its canonical name (case-insensitive)."""
        if name in self.sensors:
            return name
        low = name.lower()
        for n in self.sensors:
            if n.lower() == low:
                return n
        return name

    def is_any_admin_of_device(self, user_id: int, device_key: str) -> bool:
        dev = self.devices.get(device_key)
        if dev is None:
            return False
        return any(user_id in self.admins_of(sc.name) for sc in dev.fields.values())

    def device_topics(self, device_key: str) -> list[str]:
        dev = self.devices.get(device_key)
        if dev is None:
            return []
        if dev.topic:
            return [dev.topic]
        return [sc.topic for sc in dev.fields.values()]


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
    default_interval = int(defaults.get("interval", 300))

    sensors: dict[str, SensorConfig] = {}
    devices: dict[str, DeviceConfig] = {}
    seen_topics: set[str] = set()
    seen_names: set[str] = set()
    seen_names_lower: dict[str, str] = {}

    for dev_key, dv in raw.get("devices", {}).items():
        if dev_key in devices:
            raise ValueError(f"Duplicate device key: {dev_key!r}")

        dev_topic: Optional[str] = dv.get("topic")
        dev_interval = int(dv.get("interval", default_interval))
        dev_info = dv.get("info", dev_key)
        dev_note = dv.get("note", "")
        dev_viewers = list(dv.get("viewers", []))
        dev_admins = list(dv.get("admins", []))

        if dev_topic:
            if dev_topic in seen_topics:
                raise ValueError(f"Duplicate topic {dev_topic!r} on device {dev_key!r}")
            seen_topics.add(dev_topic)

        device_fields: dict[str, SensorConfig] = {}

        for fk, fv in dv.get("fields", {}).items():
            if fv is None:
                fv = {}

            sensor_name = f"{dev_key}_{fk}"
            if sensor_name in seen_names:
                raise ValueError(f"Duplicate sensor name derived: {sensor_name!r}")
            low = sensor_name.lower()
            if low in seen_names_lower:
                raise ValueError(
                    f"Sensor names differ only by case: {seen_names_lower[low]!r} "
                    f"and {sensor_name!r}"
                )
            seen_names.add(sensor_name)
            seen_names_lower[low] = sensor_name

            f_topic: Optional[str] = fv.get("topic", dev_topic)
            if f_topic is None:
                raise ValueError(
                    f"Field {fk!r} of device {dev_key!r} has no topic "
                    f"(neither field-level nor device-level topic defined)"
                )
            if f_topic != dev_topic:
                if f_topic in seen_topics:
                    raise ValueError(
                        f"Duplicate topic {f_topic!r} on field {dev_key!r}.{fk!r}"
                    )
                seen_topics.add(f_topic)

            if "viewers" in fv or "admins" in fv:
                f_viewers = list(fv.get("viewers", []))
                f_admins = list(fv.get("admins", []))
            else:
                f_viewers = dev_viewers[:]
                f_admins = dev_admins[:]

            sc = SensorConfig(
                name=sensor_name,
                topic=f_topic,
                json_path=fv.get("json_path") or fv.get("json_field"),
                interval=int(fv.get("interval", dev_interval)),
                info=dev_info,
                unit=fv.get("unit", ""),
                default_alarm_high=float(fv["defaultAlarmHigh"]) if "defaultAlarmHigh" in fv else None,
                default_alarm_low=float(fv["defaultAlarmLow"]) if "defaultAlarmLow" in fv else None,
                valid_min=float(fv["validMin"]) if "validMin" in fv else None,
                valid_max=float(fv["validMax"]) if "validMax" in fv else None,
                viewers=f_viewers,
                admins=f_admins,
                device_key=dev_key,
            )
            sensors[sensor_name] = sc
            device_fields[fk] = sc

        devices[dev_key] = DeviceConfig(
            key=dev_key,
            topic=dev_topic,
            interval=dev_interval,
            info=dev_info,
            note=dev_note,
            fields=device_fields,
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
        devices=devices,
        retention_days=int(defaults.get("retention_days", 30)),
        alarm_threshold_repeat=int(defaults.get("alarm_threshold_repeat", 720)),
        alarm_offline_repeat=int(defaults.get("alarm_offline_repeat", 3600)),
        debug=int(tg.get("debug", 1)),
        silent_start=bool(int(tg.get("silent_start", 0))),
        digest_time=str(tg.get("digest_time", "15:00")),
    )
