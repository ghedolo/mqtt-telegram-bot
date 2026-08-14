# ADR-0009: Blackout messages report the observation, not the cause

## Status
Proposed (memo — nothing implemented; supersedes nothing, refines ADR-0007)

## Context

ADR-0007 raises a `blackout` Alarm when *every* watched current Field is DARK
(fresh reading below `below`), sustained for `for_seconds`. The message says:

```
⚡ BLACKOUT started. (CDZ1, CDZ2 outage). CDZ1_I=0.0 CDZ2_I=0.0
```

The question that prompted this memo: is the AND across two Devices a source of
false positives, and would it be safer to report only "this Device draws no
current" and leave the blackout call to a human operator?

The answer to the first half is no, and it is worth stating plainly because the
intuition runs the other way. The AND is a **filter**: it is strictly more
restrictive than alarming on a single Field. Reporting each Device separately
would produce *more* alerts, not fewer — one per Device that goes dark on its
own — and would move the correlation work onto the operator. That option was
already considered and rejected in ADR-0007 ("Per-Field low-threshold alarms").

The real weakness is elsewhere, and it is a weakness of **wording**, not of the
state machine. The evidence the bot holds supports one statement:

> both watched loads are drawing no current, and their meters are alive.

The message asserts a different, stronger one — *there is a blackout* — which is
a **cause** inferred from that observation. The observation is compatible with at
least four causes:

1. a mains outage (the intended one);
2. a branch breaker tripped — the loads are dark, the site is not;
3. both units switched off deliberately (maintenance, seasonal shutdown, a
   weekend);
4. both current transformers failing or misreading near zero.

`for_seconds` does not separate these. A manual shutdown is sustained exactly as
well as an outage is. Everything ADR-0007 built to avoid *spurious* alarms —
end only on positive proof (`alarm_manager.py:349`), hold on UNKNOWN
(`alarm_manager.py:364`), out-of-range samples carry no evidence
(`alarm_manager.py:272`) — is sound and stays. None of it addresses cause 2, 3
or 4, because no amount of care about the current readings can distinguish
*unpowered by the grid* from *unpowered by a switch*.

Cost asymmetry matters here. A false positive costs one DM to users who
explicitly opted in via `/digest <id> on`; a false negative is a site outage
nobody hears about. That asymmetry argues for keeping the detection sensitive and
fixing the claim it makes, rather than desensitising it.

## Considered options

- **A. Leave as is.** Zero work; the machine is already conservative. Keeps a
  message that asserts a cause the data does not prove.
- **B. Per-Device "no current" only, operator decides.** No inference at all.
  Rejected: more messages, the correlation moves to the human, and the useful
  part — the co-occurrence — is thrown away. Already rejected once in ADR-0007.
- **C. Keep the AND, report the observation, add a simultaneity test
  (chosen).** Same sensitivity, no extra alerts, the human still decides but
  reads a hypothesis instead of a verdict.
- **D. An authoritative hardware signal** — a power-fail contact or a UPS
  "on battery" MQTT topic. The only option that actually proves the cause.
  Not available today (ADR-0007: the UPS Device publishes temperatures only);
  it is the right answer if the installation can be changed, and it would make
  C's inference a fallback rather than the primary signal.

## Decision

Adopt **C**, as two independent steps that can ship separately. Step 1 is worth
doing on its own; step 2 should only be built if the measurement below says it
is needed.

### Step 0 — measure first (no code)

Before changing anything, run `tools/blackout_diag.py` against the production DB
over several months and count the episodes where every watched Field was below
`below` at the same time.

- If that count matches the outages that are actually known to have happened,
  the false-positive rate is already acceptable and **step 2 is not needed** —
  ship step 1 and stop.
- If there are many more episodes than known outages, the extra ones are causes
  2–4 and step 2 earns its keep.

The local `data/sensors.db` in the repo is empty; this has to run inside the
deploy, read-only, as the tool's docstring describes.

### Step 1 — the message states what was measured

Change the blackout messages from a verdict to an observation plus a named
hypothesis, in the shape "no current on <devices>, probable blackout". The three
questions the messages already answer — who, how long, what the meters read —
stay exactly as they are, and so do the `⚡` / `🔌` markers, the alarm types
persisted in `alarms`, and the whole state machine.

This is a text change: cheap, reversible, and it removes the only claim the data
does not support. The alarm history rows keep their old wording (they are stored
strings), so old and new lines will read differently in `/lastAlarms` — that is
acceptable and needs no migration.

### Step 2 — simultaneity as the discriminator

Add an optional per-group `together_within` (seconds): raise the blackout only
if the watched Fields crossed into DARK within that window of each other.

The reasoning: a mains outage darkens every load in the same instant — well
inside one publish cadence. A manual or seasonal shutdown of two units almost
never happens within seconds. So simultaneity is the one cheap signal that
separates cause 1 from cause 3, which nothing in ADR-0007 does today.

Shape of the change: record, per Field, the timestamp of the transition into
DARK; when all Fields are DARK, require `max(t_dark) - min(t_dark) <=
together_within` before leaving SUSPECTED. Omitting the key keeps today's
behaviour exactly (no window, no test) so existing configs are unaffected.

Known limits, to be stated in the docs rather than discovered later:

- It weakens detection of a **staggered** real event (a slow brown-out where one
  meter reads zero well before the other). The window must be generous enough —
  a few multiples of the publish cadence — that this stays unlikely.
- It cannot help when a load is *already* dark before the outage starts: that
  Field has no transition inside the window. The group then holds, and the
  outage is reported late or not at all. Choosing which Fields to watch matters
  more than the window's exact value.
- A restart loses the transition timestamps, as it already loses `since`
  (`alarm_manager.py:358`). The first all-DARK evaluation after a restart should
  skip the simultaneity test rather than suppress a real alarm.

## Consequences

- Detection sensitivity is unchanged by step 1 and only narrowed, deliberately,
  by step 2. Nothing here makes the bot quieter about real outages except the
  staggered case named above.
- The operator remains the decision maker, which was the original concern — but
  reads a correlated hypothesis with the raw readings attached, instead of doing
  the correlation by hand across separate per-Device alerts.
- ADR-0007's dependency on the UPS/mains split is untouched and still the
  load-bearing assumption. If the meters ever move onto mains, or the loads onto
  UPS, everything here inverts too.
- Option D stays the real fix. If a power-fail contact or a UPS status topic ever
  becomes available, it should become the primary signal and this inference the
  fallback — at which point step 2 can be dropped.

## Prerequisite cleanup (found while writing this — since done)

`README.md` §"Understanding `stale_after`" still documented the **old**
auto-clear-on-stale behaviour: that the blackout "also auto-clears when a field
goes stale", that `stale_after` was effectively an "assume power is back after
this much silence" timeout, and an example ending in "the blackout is
auto-cleared".

The code does the opposite. `check_blackout` holds the alarm while Fields are
UNKNOWN and ends it only on positive proof (`alarm_manager.py:349-364`), which
is what README line 168, `docs/blackout-states.md` and ADR-0007 all describe.
Not counting as proof of dark is not proof of light: that is the whole point of
ending only on LIT. The section has been rewritten to match, so the baseline this
ADR proposes to change is documented correctly.

## Revisit when

- the measurement in step 0 comes back (it decides step 2);
- a third mains load gains a current Field, or the watched units start being
  switched off on a routine schedule;
- an authoritative power-fail / UPS signal becomes available (option D);
- the UPS/mains split changes in any way.

See also: [ADR-0007](0007-blackout-detection-from-current.md),
[docs/blackout-states.md](../blackout-states.md).
