import logging
import os
import yaml
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


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
    decimals: int = 1   # decimal places kept for storage/display (0-5)
    states: Optional[dict[float, str]] = None  # value→label render table (e.g. {0.0: "Aperta"})
    viewers: list[str] = field(default_factory=list)
    admins: list[str] = field(default_factory=list)
    device_key: str = ""


@dataclass
class SignalConfig:
    """A Field whose Readings are never stored — consumed only for derived
    Alarm detection (Blackout). Lives in AppConfig.signals, outside sensors, so
    every value-view (/get, /graph, /list, digest, thresholds) excludes it by
    construction. The latest value is kept only in AlarmManager's in-memory
    cache."""
    name: str           # derived: {device_key}_{field_key}
    topic: str
    json_path: Optional[str]
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
class BlackoutGroup:
    id: str                # doubles as the blackout Alarm key / digest target
    info: str              # human label shown in messages
    fields: list[str]      # canonical Sensor names watched together
    below: float           # amps; every field must read under this
    for_seconds: int       # sustained duration before raising (0 = on first dark reading)
    repeat_seconds: int    # re-notify interval while a blackout persists
    stale_after: int       # a reading older than `stale_after` seconds does not count (keep it ≥ meter publish interval)


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
    archive_time: str
    enable_menu: bool
    blackouts: dict[str, "BlackoutGroup"] = field(default_factory=dict)
    signals: dict[str, SignalConfig] = field(default_factory=dict)  # signal_name → SignalConfig
    # Non-fatal config complaints raised at load: surfaced in /sysinfo so they
    # reach a human, since a monitoring bot must never refuse to start over
    # something it can interpret.
    warnings: list[str] = field(default_factory=list)

    def _members(self, group_names: list[str]) -> set[int]:
        result: set[int] = set()
        for g in group_names:
            result.update(self.groups.get(g, []))
        return result

    def viewers_of(self, sensor: str) -> set[int]:
        sc = self.sensors.get(sensor) or self.signals.get(sensor)
        if sc is None:
            return set()
        return self._members(sc.viewers) | self._members(sc.admins)

    def admins_of(self, sensor: str) -> set[int]:
        sc = self.sensors.get(sensor) or self.signals.get(sensor)
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

    def decimals_of(self, sensor: str) -> int:
        sc = self.sensors.get(sensor)
        return sc.decimals if sc is not None else 1

    def fmt(self, sensor: str, value: float) -> str:
        """Format a value: a configured state label if the value maps to one,
        otherwise the number at the sensor's configured decimal places."""
        sc = self.sensors.get(sensor)
        if sc is not None and sc.states is not None and value in sc.states:
            return sc.states[value]
        return f"{value:.{self.decimals_of(sensor)}f}"

    def visible_sensors(self, user_id: int) -> list[str]:
        return [n for n in self.sensors if self.is_viewer(user_id, n)]

    def is_signal(self, name: str) -> bool:
        return name in self.signals

    def resolve_sensor(self, name: str) -> str:
        """Map a user-supplied sensor name to its canonical name (case-insensitive)."""
        if name in self.sensors:
            return name
        low = name.lower()
        for n in self.sensors:
            if n.lower() == low:
                return n
        return name

    def resolve_device(self, key: str) -> str:
        """Map a user-supplied device key to its canonical key (case-insensitive)."""
        if key in self.devices:
            return key
        low = key.lower()
        for k in self.devices:
            if k.lower() == low:
                return k
        return key

    def is_any_admin_of_device(self, user_id: int, device_key: str) -> bool:
        dev = self.devices.get(device_key)
        if dev is None:
            return False
        return any(user_id in self.admins_of(sc.name) for sc in dev.fields.values())

    def viewers_of_blackout(self, group_id: str) -> set[int]:
        grp = self.blackouts.get(group_id)
        if grp is None:
            return set()
        result: set[int] = set()
        for name in grp.fields:
            result |= self.viewers_of(name)
        return result

    def is_viewer_of_blackout(self, user_id: int, group_id: str) -> bool:
        return user_id in self.viewers_of_blackout(group_id)


def _load_yaml(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _collect_yaml_files(d: str) -> list[str]:
    """All *.yaml / *.yml under d, recursing into subdirectories, sorted."""
    files: list[str] = []
    for root, _dirs, names in os.walk(d):
        for n in names:
            if n.endswith((".yaml", ".yml")):
                files.append(os.path.join(root, n))
    return sorted(files)


# Only this file may carry a `defaults:` block; every other file under
# sensors.d/ must contain nothing but `devices:`.
DEFAULTS_FILE = "00-defaults.yaml"


def _load_sensors_dir(d: str) -> dict:
    """Merge every YAML file under d into one {defaults, devices} dict.

    Recurses into subfolders. `defaults:` is allowed only in DEFAULTS_FILE
    (which may also hold `devices:`); any other file may contain only a
    `devices:` block. Duplicate device keys across files are a hard error."""
    if not os.path.isdir(d):
        raise FileNotFoundError(f"Sensors directory not found: {d!r}")
    files = _collect_yaml_files(d)
    if not files:
        raise ValueError(f"No .yaml files found under {d!r}")

    defaults: dict = {}
    devices: dict = {}
    blackouts: dict = {}
    origin: dict[str, str] = {}
    for fp in files:
        data = _load_yaml(fp)
        is_defaults_file = os.path.basename(fp) == DEFAULTS_FILE
        allowed = {"devices", "defaults", "blackouts"} if is_defaults_file else {"devices"}
        extra = set(data) - allowed
        if extra:
            raise ValueError(
                f"Unexpected top-level key(s) {sorted(extra)} in {fp!r}; "
                f"only {sorted(allowed)} allowed "
                f"('defaults:' and 'blackouts:' belong in {DEFAULTS_FILE!r})"
            )
        if is_defaults_file and data.get("defaults"):
            defaults = dict(data["defaults"])
        if is_defaults_file and data.get("blackouts"):
            blackouts = dict(data["blackouts"])
        for dev_key, dv in (data.get("devices") or {}).items():
            if dev_key in devices:
                raise ValueError(
                    f"Duplicate device key {dev_key!r} in {fp!r} "
                    f"(already defined in {origin[dev_key]!r})"
                )
            devices[dev_key] = dv
            origin[dev_key] = fp
    return {"defaults": defaults, "devices": devices, "blackouts": blackouts}


def load(
    public: str = "sensors.d",
    secret: str = "credentials.yaml",
) -> AppConfig:
    raw = _load_sensors_dir(public)
    sec = _load_yaml(secret)

    defaults = raw.get("defaults", {})
    default_interval = int(defaults.get("interval", 300))

    warnings: list[str] = []
    sensors: dict[str, SensorConfig] = {}
    signals: dict[str, SignalConfig] = {}
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

            # Access lists are all-or-nothing: a Field either inherits both from
            # its Device or states both itself. Declaring one alone silently
            # blanks the other (`admins:` on its own leaves the Field with no
            # viewers), so say so — a warning, not an error, because refusing to
            # start would take the monitoring down over an access nit, which is
            # the worse outcome. The semantics are unchanged either way.
            has_viewers, has_admins = "viewers" in fv, "admins" in fv
            if has_viewers != has_admins:
                present, missing = ("viewers", "admins") if has_viewers else ("admins", "viewers")
                warnings.append(
                    f"{dev_key}.{fk}: declares {present!r} but not {missing!r} — the "
                    f"field-level list replaces the device-level one for BOTH keys, so "
                    f"{missing!r} is now empty. Add {missing}: [] to confirm, or restate "
                    f"the groups you want."
                )
            if has_viewers or has_admins:
                # `viewers:` with nothing after it parses as None, not []
                f_viewers = list(fv.get("viewers") or [])
                f_admins = list(fv.get("admins") or [])
            else:
                f_viewers = dev_viewers[:]
                f_admins = dev_admins[:]

            if fv.get("signal"):
                # A Signal: never stored, consumed only for blackout detection.
                # Kept out of `sensors`/`device_fields` so all value-views and
                # the per-device offline check ignore it. It still claimed its
                # name and topic above, so collisions are caught like any field.
                signals[sensor_name] = SignalConfig(
                    name=sensor_name,
                    topic=f_topic,
                    json_path=fv.get("json_path") or fv.get("json_field"),
                    viewers=f_viewers,
                    admins=f_admins,
                    device_key=dev_key,
                )
                continue

            decimals = int(fv.get("decimals", 1))
            if not 0 <= decimals <= 5:
                raise ValueError(
                    f"Field {fk!r} of device {dev_key!r}: decimals must be 0-5, got {decimals}"
                )

            states = None
            if "states" in fv:
                # Readings are stored as floats, so normalise every key form
                # (bool false/true, int 0/1, str "0"/"1") to a float key. This
                # renders discrete values (e.g. a door contact) as labels while
                # the stored value stays numeric.
                try:
                    states = {float(k): str(v) for k, v in fv["states"].items()}
                except (TypeError, ValueError, AttributeError):
                    raise ValueError(
                        f"Field {fk!r} of device {dev_key!r}: 'states' must map "
                        f"numeric keys to labels"
                    )

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
                decimals=decimals,
                states=states,
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

    default_repeat = int(defaults.get("alarm_offline_repeat", 3600))
    blackouts: dict[str, BlackoutGroup] = {}
    for gid, gv in (raw.get("blackouts") or {}).items():
        gv = gv or {}
        if gid in sensors:
            raise ValueError(
                f"Blackout group {gid!r} collides with a sensor name"
            )
        g_fields = list(gv.get("fields", []))
        if not g_fields:
            raise ValueError(f"Blackout group {gid!r}: 'fields' is required and non-empty")
        for fn in g_fields:
            if fn not in sensors and fn not in signals:
                raise ValueError(f"Blackout group {gid!r}: unknown field {fn!r}")
        if "below" not in gv:
            raise ValueError(f"Blackout group {gid!r}: 'below' is required")
        below = float(gv["below"])
        for_seconds = int(gv.get("for_seconds", 10))
        stale_after = int(gv.get("stale_after", 15))
        if below <= 0:
            raise ValueError(f"Blackout group {gid!r}: 'below' must be > 0")
        if for_seconds < 0:
            raise ValueError(f"Blackout group {gid!r}: 'for_seconds' must be >= 0")
        if stale_after <= 0:
            raise ValueError(f"Blackout group {gid!r}: 'stale_after' must be > 0")
        blackouts[gid] = BlackoutGroup(
            id=gid,
            info=gv.get("info", gid),
            fields=g_fields,
            below=below,
            for_seconds=for_seconds,
            repeat_seconds=int(gv.get("repeat_seconds", default_repeat)),
            stale_after=stale_after,
        )

    for w in warnings:
        log.warning("config: %s", w)

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
        archive_time=str(defaults.get("archive_time", "12:00")),
        enable_menu=bool(int(tg.get("enableMenu", 1))),
        blackouts=blackouts,
        signals=signals,
        warnings=warnings,
    )
