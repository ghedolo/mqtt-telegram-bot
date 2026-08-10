# Code review with a second model

This document records one experiment. On 9 and 10 August 2026 we reviewed this
project with two different models at the same time. One model read the code and
reported problems. The other model checked each report, wrote the corrections,
and wrote the tests.

The text uses simple English on purpose. One idea per sentence. No marketing
words.

## How the work was divided

- **Reviewer: Claude Fable 5.** It ran as a separate agent. It could read files
  and search. It could not change files. It reported problems only.
- **Verifier and author: Claude Opus 5.** It ran the main session. It checked
  every reported problem against the code. It rejected the reports it did not
  agree with. It wrote all corrections, all tests, and all documentation.

The reviewer started each pass with no memory of the session. It did not know
which code was new and which code was old. It did not know the intent behind
any line.

## The four passes

| Pass | Files reviewed | Problems reported |
|---|---|---:|
| 1 | `bot/telegram_bot.py` (1614 lines) | 11 |
| 2 | `bot/config.py`, `bot/db.py` | 12 |
| 3 | `bot/alarm_manager.py`, `bot/mqtt_client.py`, `bot/ingest.py`, `bot/main.py` | 6 |
| 4 | All documents against all code and all tests | 10 |

Each pass received the domain documents (`CONTEXT.md`, `README.md`,
`docs/permissions.md`) as the specification, and the existing tests as the
record of what was already covered.

## Cost in tokens

These numbers come from the agent tool itself. They count the reviewer only.

| Pass | Tokens | Tool calls | Run time |
|---|---:|---:|---:|
| 1 | 136,383 | 9 | 7 min |
| 2 | 95,496 | 7 | 5 min |
| 3 | 126,644 | 26 | 12 min |
| 4 | 193,921 | 62 | 36 min |
| **Total** | **552,444** | **104** | **60 min** |

Two limits apply to these numbers:

1. They do not include the main session. The verification, the corrections, the
   tests and the documentation all cost extra tokens, and we did not measure
   them separately.
2. They are tokens, not money. The price per token depends on the plan.

The main session received only the short reports. The 552,444 tokens of file
reading stayed outside it. This is the main practical gain: a long review does
not fill the working session.

## Result

| Outcome | Count |
|---|---:|
| Problems reported | 39 |
| Corrected | 35 |
| Rejected after checking | 3 |
| Left open by decision | 1 |

The test suite grew from 240 tests to 298 tests during the session. The four
review passes added 42 of those tests. Every new test failed before its
correction and passed after it. We checked this each time with a temporary undo
of the source change.

The corrections shipped in five commits and took the version from 1.6 to 1.6.3.

### The four most serious faults

**1. A database migration deleted data.** `bot/db.py` rebuilds the `thresholds`
table to remove an old `NOT NULL` rule. The rebuild copied two columns and then
dropped the old table. The third column, `low`, was not copied. Every low alarm
threshold was lost at the first start after an upgrade. The loss was silent and
permanent. We reproduced the fault on a copy of the old schema before we
corrected it.

**2. Two alarm types were invisible.** The `alarms` table stores three kinds of
subject: a sensor name for threshold alarms, a device key for offline alarms,
and a group id for blackout alarms. Both history commands built their query from
the sensor list only. The offline history and the blackout history were written
to the database and never read back. No command in the bot could show them.

**3. One reset cleared the wrong alarm.** A change to the high threshold also
cleared the state of an active low alarm. The recovery message depends on that
state. So the low alarm ended without its green message, or returned as a new
alarm on the next reading.

**4. A command told strangers which devices exist.** `/ackOff` answered
"Unknown device." before it checked access. A user with no access could send
guesses and separate real device keys from invented ones by the answer.

### The rest

Corrections in the configuration loader and in the two manual scripts. Examples: a
publish interval of zero was accepted and made the offline rule permanent; a
minimum limit above the maximum limit was accepted and stopped all alarms on
that field; a quoted `"false"` for TLS was read as *true*; the rename script did
not rename the `mutes` table, so a user's silence setting stopped working after
a device rename.

Nine corrections in the Telegram layer: prompt records that could mix two chats,
a listing that failed above the Telegram message limit, a message with no sender
that stopped a command with an error, and thresholds shown in a format that did
not match the value columns.

Eight corrections in the documents. The most important one: `CONTEXT.md`
described a daily digest format that the code never produced.

### Rejected after checking

Three reports were correct as observations but were not faults:

- A mute check also deletes expired rows. All database writes happen on one
  thread, so this is untidy, not dangerous.
- No connection pool and no write-ahead log. Same reason.
- Two separate threshold reads. The callers that need both already use the bulk
  query.

### Left open

TLS certificate checking is disabled in `bot/mqtt_client.py`. The traffic is
encrypted, but the broker identity is not verified. The correction depends on
the certificate the broker uses, so the operator must decide.

## What we can claim, and what we cannot

We can claim this:

- **No invented faults.** Every report pointed at real code. We checked all of
  them. The severity was sometimes optimistic, but the observation was always
  true.
- **A second reader found old faults.** The migration fault had been in the code
  for several releases. The hidden offline history had been there since offline
  alarms were added. Both survived normal work because nobody read those lines
  again with fresh eyes.
- **Cost separation works.** One hour of reading cost the main session almost
  nothing in context.
- **The two roles are different.** The reviewer reads and reports. It cannot
  reproduce a fault, weigh a risk against the project style, or write a test
  that fails first. Those steps came from the main model.

We cannot claim this:

- This was not a controlled comparison. We did not run the same four passes with
  Opus alone and compare the results. So we cannot say that one model finds more
  faults than the other.
- The quality of the final result comes from both models together. The reviewer
  raised 39 items. Four of them mattered a lot. The judgement about which four
  came from the verification step, not from the report.

## Practical advice for the next time

1. Give the reviewer the domain documents as the specification. Several reports
   were valuable exactly because the code disagreed with `CONTEXT.md` or
   `docs/permissions.md`.
2. Ask for a failure scenario with every report. This removes style opinions and
   makes verification fast.
3. Check every report before you correct anything. Three of 39 did not survive.
4. Write the test before the correction, and confirm that the test fails.
   Without that step you cannot tell a real correction from a comfortable one.
5. Expect line numbers to drift. The reviewer reads a file that the main session
   may change minutes later.
6. Keep one pass for documents only. That pass found the second worst fault of
   the whole review, in a script, not in the bot.
