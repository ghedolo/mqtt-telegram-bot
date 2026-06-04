# ADR-0002: Group membership for Users, chat_id whitelist for Admins

## Status
Accepted

## Context
Two access levels needed: read-only Users and privileged Admins. Options were full chat_id whitelist for both, or Telegram Group membership as the user gate.

## Decision
- **Users**: anyone in the Telegram Group can issue read-only commands (`get`, `graph`). Access managed by controlling group invitations.
- **Admins**: explicit chat_id list in config. Required to use `set`, silence, and other privileged commands.

## Consequences
- No per-user onboarding friction for read-only access
- Admin access is explicit and auditable via config
- Risk: if group invite link leaks, unknown users can read sensor data (acceptable for this use case)
