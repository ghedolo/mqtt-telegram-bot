# ADR-0001: Telegram over Microsoft Teams

## Status
Accepted

## Context
Sensor data needs to be distributed to colleagues with interactive command support. Microsoft Teams is the company standard but app registration in Azure AD is blocked for regular users — creating a bot requires IT admin involvement.

## Decision
Use Telegram. A bot can be created and deployed by a single user with no IT dependency.

## Consequences
- No IT approval needed
- Users access via personal Telegram accounts, not company accounts
- Access control via Telegram Group membership (for users) and chat_id whitelist (for admins)
