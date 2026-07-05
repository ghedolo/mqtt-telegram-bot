# ADR-0007: Blackout detection as a derived Alarm from current Fields

## Status
Accepted (design agreed; not yet implemented)

## Context
Some Devices (the `SM1_UTA*` air-handling units) gained a current Field `I` (topic `R2Env/cdz/I*`, amps). Because the whole monitoring stack — sensors, MQTT broker, the bot's Docker host, the network — runs on UPS while the measured loads (the CDZ units) run on mains, a mains blackout produces a distinctive, observable signature: the current meters keep publishing on UPS power, and every CDZ current drops to a near-zero value. A live-but-idle CDZ still draws a baseline of ~1.6 A (compressor off, electronics on), so a reading well below that baseline means *unpowered*, not merely idle — the threshold alone separates the two cases.

We want an alert when the site loses power, plus an end-of-blackout notification when it returns. The UPS Device exposes only temperatures, no "on battery" status topic, so an authoritative UPS-derived signal is not available; inference from CDZ current is the best signal we have.

## Considered options
- **Virtual/computed Sensor.** Model the blackout as a synthetic Field whose value is derived from the current Fields, reusing thresholds/graph/digest. Rejected: the data model is strictly one Field ↔ one MQTT Topic; a computed Field is a new concept in the ingestion path, and it would write fake Readings into `readings`/`readings_archive`.
- **Per-Field low-threshold alarms.** Set a low Threshold on each `I`. Rejected: the system has no AND-composition across Fields, so users would get two independent "no current" alarms instead of one blackout, and a single unit losing power (breaker) is not a site blackout.
- **Derived Alarm type (chosen).** A new `blackout` Alarm evaluated in the same periodic loop as offline.

## Decision
Model Blackout as a third Alarm type, sibling of `offline` — a derived, temporal condition, not a stored Reading.

- **Config.** A `blackouts:` block, allowed only in `00-defaults.yaml` (the sole file permitted non-`devices` top-level keys). It is a map of Blackout Group id → rule: `fields` (canonical Sensor names to watch), `below` (amps; all must be under it), `for_seconds` (sustained duration before raising, may be 0), `stale_after` (freshness window), `repeat_seconds` (re-notify interval, default = `alarm_offline_repeat`). Startup validation hard-errors on unknown Field names.
- **Evaluation.** Event-driven: `check_blackout_for(sensor)` runs from `on_reading` on every incoming current reading, so detection latency is roughly the meter's publish cadence (not tied to the 60 s offline loop, which brief outages would outrun). A watched Field counts as "dark" when its latest reading is below `below` **and** newer than `stale_after`; if all are dark, sustained for `for_seconds`, a blackout Alarm keyed by the group id is raised, repeating no more often than `repeat_seconds`, and auto-clears (with a recovery message) when any Field rises above the threshold or goes stale. **`for_seconds` (sustain) and `stale_after` (freshness) are decoupled** — an earlier version conflated them into one, which made a small `for_seconds` silently unobservable whenever it fell below the meter's publish interval.
- **Subscription (opt-in, Model B).** The Blackout Group id is a subscribable pseudo-entity: a User opts in with `/digest <id> on`. The id is a valid `/digest` target but carries no Reading, so it never renders as a value row in `/get` or the daily digest. Notification is DM to subscribed Users only.
- **Authorization.** Any Viewer of at least one watched Field may subscribe — deliberately more permissive than offline (Admin-gated), because subscription is an explicit, self-selected opt-in with no spam risk, and a climate blackout concerns room occupants (Viewers), not just operators.

## Consequences
- Blackout inference depends on the UPS/mains split staying as-is: if the current meters were ever moved onto mains (or the CDZ onto UPS) the signal inverts and this detection breaks. If the blackout outlasts UPS runtime, the bot itself dies — no alert is possible past that point.
- Adds a `blackout` value to the Alarm type set persisted in `alarms`; keyed by group id (a non-Device, non-Sensor key), like offline is keyed by Device key.
- `/digest` and Sensor-name resolution must accept Blackout Group ids as targets while treating them as value-less — a small, explicit exception.
- The threshold `below` must stay comfortably under the idle baseline (~1.6 A); if a different CDZ model idles lower, the group's `below` needs tuning.
- No fake Readings are stored; `/graph` of a blackout is not possible (the underlying currents are graphable on their own).
