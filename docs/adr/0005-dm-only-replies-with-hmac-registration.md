# ADR-0005: DM-only replies with HMAC token registration

## Status
Accepted

## Context
With per-sensor visibility (ADR-0004), replying to commands in the Telegram Group would expose one user's sensor data to all group members. Notifications (Alarms, Daily Digest) must also be scoped per user.

The Telegram API requires a user to have initiated a DM conversation with the bot before the bot can send them private messages. A mechanism is needed to bootstrap this channel without requiring manual out-of-band setup.

Two notification delivery options were considered:
- Keep notifications in the Telegram Group (acceptable data leakage, simpler).
- Send all sensor data exclusively via DM (no leakage, requires registration flow).

For DM registration, a plain deep link (`t.me/botname?start=ok`) would allow any user — including unauthorized ones — to register. A signed token prevents this.

## Decision
All sensor data (command replies, Alarms, Daily Digest) is sent exclusively via DM. The Telegram Group is used only for DM Registration prompts.

When the bot needs to DM a user whose channel is not yet open, it sends a message in the Telegram Group containing an HMAC-signed deep link. The token encodes the target `chat_id` and a timestamp; TTL is 24 hours. On `/start <token>`, the bot verifies the HMAC and that the sender's `chat_id` matches the token's encoded target before registering the DM channel.

## Consequences
- No sensor data ever appears in the shared Telegram Group.
- Users must complete DM Registration before receiving notifications or command replies.
- HMAC verification prevents link sharing: only the intended user can complete registration with a given token.
- Failed DM sends (channel not open) surface as a visible registration prompt in the Group rather than silent data loss.
- Unauthorized users who somehow obtain a valid token cannot use it: the HMAC encodes their specific `chat_id`.
