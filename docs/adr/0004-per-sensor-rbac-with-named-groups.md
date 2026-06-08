# ADR-0004: Per-sensor RBAC with named Access Groups

## Status
Accepted

## Context
Sensor data needs to be visible only to users who have a legitimate need to see it. The original model granted read access to all sensors to anyone in the Telegram Group. As the number of sensors and users grew, different user populations need visibility into different sensor subsets.

Two alternatives were considered:
- **Flat per-sensor lists**: each sensor carries its own `viewers` and `admins` chat_id lists directly.
- **Named Access Groups**: groups are defined centrally in credentials config; sensors reference group names.

A global Admin role was also considered but rejected: per-sensor admin scope allows finer-grained delegation and avoids a single "god" account.

## Decision
Use named Access Groups. Groups are defined in credentials config as `name → [chat_ids]`. Each sensor in Sensor Config references zero or more group names under `viewers` and `admins`. Admin implies Viewer for the same sensor. Sensors without any Access Group assignment are visible to nobody (fail-closed).

## Consequences
- Adding a user to all sensors of a team requires editing only one group definition, not every sensor entry.
- Per-sensor admin scope enables delegation without granting global privileges.
- Sensors omitted from all groups are inaccessible — protects against accidental exposure on config migration.
- A user with no group membership receives "sensor not found" for all commands, revealing nothing about which sensors exist.
- Replaces ADR-0002's Telegram Group membership gate: access is now entirely config-driven, independent of Telegram Group membership.
