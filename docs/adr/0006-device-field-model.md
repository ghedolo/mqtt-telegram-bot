# ADR-0006: Device/Field model for multi-value sensors

## Status
Accepted

## Context
Some physical devices publish multiple measured values (temperature, humidity, battery) on a single MQTT topic as a JSON payload. The original model treated each value as a separate Sensor with the same topic but a different `json_field` extraction path. This caused two bugs and one design gap:

1. **Silent data loss**: `MqttClient._topic_map` was built as `dict[str, SensorConfig]`, keyed by topic. Sensors sharing a topic silently overwrote each other at startup — only the last-registered sensor received readings.
2. **False offline alarms**: offline detection was per-sensor (field). A device that publishes intermittently (frequency varies with conditions, or some fields absent from some messages) would trigger spurious offline alarms for fields not present in every message.
3. **Redundant alarms**: a device going offline would generate one offline alarm per field, all identical in meaning.

Additionally, some devices publish JSON fields whose presence varies per message (not every message contains all fields), making a per-field interval timeout unreliable.

## Decision
Introduce a two-level model:

- **Device**: identified by a key in `sensors.yaml` (under `devices:`). Owns a Topic and an interval. Offline detection is per-Device: if no MQTT message arrives on the Topic for 3× the Device interval, one offline alarm is raised for the Device.
- **Field**: a measurable value within a Device, identified by a key nested under `fields:`. The canonical Sensor name used in DB keys and bot commands is derived as `{device_key}_{field_key}`. A Device with a single Field is valid and common.

Rules:
- `viewers`/`admins` defined at Device level serve as defaults; Field-level definitions fully replace (not merge) the Device defaults.
- Offline alarm recipients: Admins of Fields for which the User has an active `/digest` subscription in that Device.
- `MqttClient._topic_map` becomes `dict[str, list[SensorConfig]]`: one MQTT subscribe per unique Topic, dispatching to all Fields of that Device.
- Sensor names derived as `{device_key}_{field_key}` with `_` separator preserve all existing DB keys — no data migration required.
- Config validation at load enforces: unique Device keys, unique Topics across Devices, unique derived Sensor names.

## Consequences
- Eliminates silent data loss: all fields of a multi-value device receive readings from every message.
- Eliminates false offline alarms: liveness is tracked per-Topic (device), not per-field.
- Reduces alarm noise: one offline alarm per Device instead of one per Field.
- `/list` output groups all Fields of a Device on one line: `info: F1=v1unit F2=v2unit ...`
- `/ackOff` and `/forgetSensor` take a Device key, not a Field/Sensor name.
- `sensors.yaml` requires migration from flat `sensors:` to nested `devices: / fields:` structure. Existing Sensor names are preserved if Field keys match the suffix of existing names.
- Single-field Devices are valid — the model is fully backward-compatible in semantics.
