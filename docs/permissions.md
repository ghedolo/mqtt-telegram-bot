# Permissions — users, groups, roles, actions

Who can do what, and where each rule lives in the code. Derived from
`bot/config.py` (`AppConfig` predicates) and the handler gates in
`bot/telegram_bot.py`. `CONTEXT.md` defines the vocabulary; this file is the
operational matrix.

> **Field or Sensor — one word, not two.** They are the same object named at
> two stages: `T` is the Field key inside device `SM1` in YAML, `SM1_T` is the
> Sensor name it derives — what you type in commands and what every permission
> check takes. This document says **Sensor** throughout, and keeps *Field* only
> in §2, where the subject is literally the YAML `fields:` block.

## 1. The four things

| Concept | What it is | Where it is declared |
|---|---|---|
| **User** | a Telegram numeric user id | nowhere by itself — a user exists to the bot only by appearing in an Access Group or in `superadmin` |
| **Access Group** | a named list of user ids | `credentials.yaml`, under `groups:` |
| **Role** | `viewer` or `admin`, **per Sensor** | `sensors.d/*.yaml`, as `viewers:` / `admins:` on a Device or a Sensor — the values are Access Group *names*, never raw ids |
| **Superadmin** | a flat list of user ids | `credentials.yaml`, under `superadmin:` |

Two consequences that catch people out:

- **A role is never global.** "Admin" always means *admin of a specific Sensor*.
  The same user can be admin of `SM1_T`, viewer of `SM2_H`, and invisible to
  `SM3_IF`.
- **Superadmin is orthogonal, not a promotion.** It is a separate list, not an
  Access Group, and it grants no view or admin rights on any Sensor. A
  superadmin who is in no Access Group sees no data at all — `/dbStats` works,
  `/get` returns nothing.

## 2. How a role reaches a Field

```
credentials.yaml            sensors.d/*.yaml
  groups:                     devices:
    ops:      [111, 222] ◄──    SM1:
    ops2:     [444]      ◄──      viewers: [watchers]   ← device default
    watchers: [333]      ◄──      admins:  [ops]        ← device default
                                  fields:
                                    T: {}               ← inherits both
                                    H:
                                      viewers: []       ← REPLACES, does not merge
                                      admins: [ops2]    ← both keys required together
```

Resulting access:

| Sensor | Admins | Viewers (admins included) |
|---|---|---|
| `SM1_T` | `ops` → 111, 222 | + `watchers` → 333 |
| `SM1_H` | `ops2` → **444 only** | **444 only** — 111, 222 and 333 lost this Sensor |

Resolution rules (`bot/config.py:87-115`, `bot/config.py:312-317`):

- Field-level `viewers`/`admins` **fully replace** the Device-level lists.
  There is no merging, and the pair is **all-or-nothing**: a Field states both
  keys or neither. Declaring only one still loads, with the unstated key
  blanked as always, but raises a config warning —

  ```
  ⚠️ config: SM1.H: declares 'admins' but not 'viewers' — the field-level list
  replaces the device-level one for BOTH keys, so 'viewers' is now empty.
  Add viewers: [] to confirm, or restate the groups you want.
  ```

  It is a warning and not a load error on purpose: a monitoring bot that
  refuses to start leaves *everything* unwatched, which beats any access
  mistake it might have prevented. Warnings appear in the log, at the bottom of
  `/sysinfo`, and in the `/reloadConfig` reply — the reload being the moment
  the author is actually looking.

  So in the sketch `SM1_H` gives up `watchers` *and* `ops`: `viewers: []` says
  that out loud instead of leaving it to be inferred. To keep the device
  defaults *and* add someone, restate every group you still want.
- `viewers_of(sensor) = members(viewers) ∪ members(admins)` — **Admin implies
  Viewer**. You never list a group in both.
- A Field with no `viewers` and no `admins` anywhere in its chain is visible to
  **nobody**. Fail-closed, by design.
- A **Signal** (`signal: true`) inherits its Device's lists like any Field, and
  those govern who may subscribe to the Blackout Group it feeds.

Derived predicates:

| Predicate | Meaning |
|---|---|
| `is_viewer(u, s)` | `u` may read Sensor `s` |
| `is_admin(u, s)` | `u` may change Sensor `s` |
| `is_any_admin(u)` | `u` is admin of **at least one** Sensor — unlocks the 72h export window and the admin section of `/help` |
| `is_any_admin_of_device(u, dev)` | `u` is admin of at least one Sensor of `dev` — gates `/ackOff` |
| `is_viewer_of_blackout(u, gid)` | `u` is viewer of at least one Sensor watched by group `gid` |
| `is_superadmin(u)` | `u` is in the `superadmin:` list |

## 3. Roles × entities

Four kinds of thing carry permissions:

| | Device (`SM1`) | Sensor (`SM1_T`) | Signal (`SM3_IF`) | Blackout Group (`R2`) |
|---|---|---|---|---|
| **carries a role?** | no — inherited from its Sensors | **yes, the only place roles are enforced** | yes, inherited from its Device | no — derived from the Sensors it watches |
| **has stored Readings?** | — | yes | **never** | no (not a Reading at all) |
| **has Thresholds?** | — | yes | no | n/a (own threshold lives in config) |

In the tables below, `—` = not available to anyone at that level. Every row
also requires DM Registration.

### Actions on a Sensor

| Action | Non-user | Viewer | Admin | Superadmin |
|---|---|---|---|---|
| see it exist at all (`/list`, `/get`) | — | ✅ | ✅ | — *(unless also a Viewer)* |
| read value, threshold, history (`/get`, `/getAlarm`, `/lastAlarms`, `/last5Alarm`) | — | ✅ | ✅ | — |
| chart / export (`/graph`, `/csv`, `/xlsx`) | — | ✅ max 24h | ✅ max 72h | — |
| set / clear thresholds (`/setAlarm`, `/setAlarmLow`, `/clearAlarm`, `/clearAlarmLow`) | — | ❌ *"Not authorized"* | ✅ | ❌ |
| receive threshold alarm DM | — | ✅ unless muted | ✅ unless muted | — |
| mute own alarm DMs (`/silent`) | — | ✅ | ✅ | — |
| subscribe to digest (`/digest`) | — | ✅ | ✅ | — |

### Actions on a Device

| Action | Non-user | Viewer | Admin | Superadmin |
|---|---|---|---|---|
| see it in `/list` | — | ✅ *(only its visible Sensors)* | ✅ | — |
| acknowledge offline alarm (`/ackOff <dev>`) | — | ❌ | ✅ *(admin of any one Sensor)* | ❌ |
| list active acks (`/ackOff`, no arg) | ✅ **any DM-registered user, system-wide** | ✅ | ✅ | ✅ |
| receive offline alarm DM | — | ❌ | ✅ **and** subscribed to that Sensor | — |
| wipe to history (`/forgetSensor <dev>`) | — | ❌ | ❌ | ✅ |

### Actions on a Signal

| Action | Non-user | Viewer | Admin | Superadmin |
|---|---|---|---|---|
| appears in `/get`, `/graph`, `/list`, digest, thresholds | **never — for anybody**, by design | never | never | never |
| see its name (`/listSignal`) | — | ✅ | ✅ | — |
| see its **live value** (`/listSignal`) | — | ❌ *(name only)* | ✅ `= 1.7 (3s ago)` | — |

### Actions on a Blackout Group

| Action | Non-user | Viewer | Admin | Superadmin |
|---|---|---|---|---|
| see it (`/list` tail, `/listSignal`) | — | ✅ *(viewer of ≥1 watched Sensor)* | ✅ | — |
| subscribe / unsubscribe (`/digest <id> on\|off`) | — | ✅ | ✅ | — |
| receive blackout DM | — | ✅ **and** subscribed | ✅ **and** subscribed | — |

### Actions with no entity

| Action | Non-user | Viewer | Admin | Superadmin |
|---|---|---|---|---|
| `/sysinfo`, `/last`, `/myid`, `/help`, `/exprSyntax` | ✅ | ✅ | ✅ | ✅ |
| `/reloadConfig`, `/usersActivity`, `/dbStats` | ❌ | ❌ | ❌ | ✅ |

### Reading the table

- **Admin ⊃ Viewer**, always: `viewers_of()` unions both lists, so an admin
  never needs listing in `viewers:`.
- **Superadmin ⊅ Viewer.** The `—` in the superadmin column is not an
  oversight: superadmin is a separate list of ids and grants zero read access.
  A superadmin sees Sensors only where they *also* appear in an Access Group.
- **Signal privacy is two-tiered**: any viewer of the feeding Sensor learns the
  Signal *exists* (so they can decide whether to subscribe to the group), only
  an admin sees the sampled current.
- **Alarm delivery widens as it goes**: threshold → all Viewers automatically;
  offline → Admins, and only if subscribed; blackout → any Viewer, and only if
  subscribed. Two of the three are opt-in.
- **"Non-user" is not "stranger".** Anyone who opens a DM with the bot is
  registered on the spot (`_get_reply_chat` calls `register_dm` when the chat
  *is* the user), with no group membership required. So the ✅ marks in that
  column are reachable by any Telegram user who finds the bot: `/sysinfo`,
  `/last`, and the argument-less `/ackOff` listing. They expose health metrics
  and device keys, never Readings.

## 4. Gate that runs before everything

`_get_reply_chat` (`bot/telegram_bot.py:221`) runs first in nearly every
handler. If the user has not completed **DM Registration**, the command is
dropped and a registration prompt with an HMAC deep link goes to the Telegram
Group instead. So the real precondition chain is:

```
DM registered → in an Access Group → viewer of the Sensor → admin of the Sensor
```

Exceptions that skip `_get_reply_chat` and answer in whatever chat they were
typed in: `/start`, `/myid`, `/help`.

## 5. Command matrix

**Visibility scope** describes what the command operates on, *after* the role
check passes. "Visible Sensors" means `visible_sensors(user)` — silently empty
for a user in no Access Group.

### Open to any DM-registered user

| Command | Role needed | Visibility scope |
|---|---|---|
| `/start` | none | DM registration itself (HMAC token must match the sender) |
| `/myid` | none | — |
| `/help` | none | admin/superadmin sections appended per `is_any_admin` / `is_superadmin` |
| `/sysinfo` | none | global, non-sensitive health only |
| `/last` | none | global MQTT timestamp, no content |
| `/exprSyntax` | none | — |
| `/list` | viewer | visible Sensors + visible Blackout Groups |
| `/get` | viewer | visible Sensors |
| `/getAlarm` | viewer of the named Sensor | visible Sensors |
| `/graph`, `/csv`, `/xlsx` | viewer | visible Sensors; window max 24h, **72h if `is_any_admin`** |
| `/lastAlarms` | viewer | visible Sensors (subscriptions when no expr) |
| `/last5Alarm` | viewer of the named Sensor | one Sensor |
| `/digest` | viewer | visible Sensors + Blackout Groups the user may view |
| `/silent` | viewer | own mutes only, per-user |
| `/listSignal` | viewer of a watched Sensor | Blackout Groups the user may view; live Signal value shown only to Admins |

### Admin of the affected Sensor

| Command | Gate |
|---|---|
| `/setAlarm <field> <value>` | `is_viewer` then `is_admin` on that Sensor |
| `/setAlarmLow <field> <value>` | same |
| `/clearAlarm <field>` | same |
| `/clearAlarmLow <field>` | same |
| `/ackOff <device>` | `is_any_admin_of_device` |

The two-step check is deliberate: a non-viewer gets `Unknown sensor.` and a
viewer-without-admin gets `Not authorized.`, so a user never learns that a
Sensor exists outside their visibility.

`/ackOff` **without arguments** lists every active offline ack system-wide and
is reachable by any DM-registered user — the only place where a non-admin sees
device state beyond their own visibility. Known wart, listed here rather than
silently tolerated.

### Superadmin

| Command | Gate |
|---|---|
| `/forgetSensor <device>` | `is_superadmin` |
| `/reloadConfig` | `is_superadmin` |
| `/usersActivity` | `is_superadmin` |
| `/dbStats` | `is_superadmin` |

## 6. Notification fan-out

Delivery follows the same roles, with an extra opt-in layer. Nothing is ever
sent to the Telegram Group except registration prompts.

| Event | Recipients | Extra conditions |
|---|---|---|
| Threshold alarm | **Viewers** of the Sensor (`viewers_of`, so admins too) | DM registered, and not muted via `/silent` |
| Offline alarm | **Admins** of the Device's Sensors | DM registered, **and** subscribed to that Sensor via `/digest` |
| Blackout alarm | **Viewers** of a watched Sensor | DM registered, **and** subscribed to the Blackout Group id via `/digest` |
| Daily digest | each user | their `/digest` subscriptions, intersected with what they may view |

Offline is Admin-gated *and* subscription-gated — the narrowest of the three.
Blackout is the widest by role (any viewer) but strictly opt-in.

## 7. Autocomplete menu is not a permission boundary

`set_my_commands` advertises **user-level commands only**. Admin and superadmin
commands are absent from autocomplete but their handlers still run when typed —
the handler gate is the only thing enforcing access. Hiding a command from the
menu protects nothing; removing its gate would.

The split is pinned by tests (`MENU_EXEMPT` in `tests/test_telegram.py`, see
`docs/TESTING.md`), so a new user-level command cannot silently miss the menu
and a privileged one cannot leak into it.

## 8. Adding a user — checklist

1. User sends `/myid` in the Telegram Group, reports the number.
2. Add the id to an Access Group under `groups:` in `credentials.yaml`, or
   create a group for them.
3. Reference that group as `viewers:` or `admins:` on the Devices/Sensors they
   need, in `sensors.d/`. Remember: a Sensor-level list replaces the Device-level
   one entirely.
4. `/reloadConfig` (superadmin) — no restart needed.
5. The user clicks the registration deep link the bot posts in the Group, so
   DMs can reach them. Until then every command answers with a prompt, not data.

To grant superadmin instead, add the id to `superadmin:` — and remember that
alone gives no visibility of any Sensor.
