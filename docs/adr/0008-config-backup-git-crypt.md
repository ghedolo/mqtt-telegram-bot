# ADR-0008: Encrypted backup of config in a separate git-crypt repo

## Status

Accepted (setup steps in [docs/backup-config.md](../backup-config.md); performed on production).

## Context

`sensors.d/` and `credentials.yaml` are gitignored by the code repo and live
only on the production host (bind-mounted into the container). There is no
backup: if the host's disk dies, the whole topology and the credentials are
lost. We want versioned, restorable backup.

Two properties constrain the solution:

- `credentials.yaml` holds **live secrets** (Telegram bot token, MQTT password)
  and **PII** (user chat_ids). `sensors.d/` holds only topology and *group
  names*, no secrets.
- The bot must read a **plaintext** `credentials.yaml` at runtime (bind mount).
- The developer's Mac must **never** hold the secrets; backup is operated only
  from production.

## Considered options

- **Un-ignore and commit into the code repo.** Rejected: the code repo is
  shared, and this would put a live token + PII into its (public-ish) history
  forever.
- **Plaintext in a separate private repo.** Access-control only. Rejected: a
  private repo is cloned to laptops, its access list drifts, the host stores it
  in cleartext, and history is forever — a live token deserves encryption at
  rest, not just an ACL.
- **SOPS + age** (encrypt values in YAML). Nice diffs, but not transparent: the
  bot needs a plaintext file, so the deploy would need an extra `sops -d` step
  producing that file. Rejected for the added moving part.
- **age blob** (encrypt everything into one `.age`). Simplest as a dumb backup,
  but loses per-file versioning and diffs. Rejected.
- **git-crypt in a separate private repo (chosen).**

## Decision

A private GitHub repo `lortebot-config`, a **sibling** of the deploy dir, holds
`sensors.d/` (plaintext) and `credentials.yaml` (encrypted with **git-crypt**
via `.gitattributes`). git-crypt is transparent — the working tree is the
plaintext the bot bind-mounts — so no deploy step changes.

- **Scope.** Only `credentials.yaml` is encrypted; `sensors.d/` stays plaintext
  so its config history remains diff-able (it has no secrets).
- **Host.** GitHub private repo. Since the content is encrypted, GitHub-vs-
  self-hosted-GitLab is a policy choice, not a security one.
- **Key custody.** A git-crypt **symmetric key**, exported and base64-encoded
  into a ~200-char text string, stored in a password manager (which only holds
  text). It is the single recovery artifact and must outlive the host; a second
  offline copy is advised.
- **Wiring.** `docker-compose.yml` mounts `${LORTE_CONFIG:-.}/sensors.d` and
  `.../credentials.yaml`; production sets `LORTE_CONFIG=../lortebot-config` in
  `bot/.env`. Default `.` preserves the legacy in-place layout. Only the
  subpaths are mounted, so `.git/` (and the key) never enters the container.

## Consequences

- Two independent git flows: **code** authored on the Mac and pushed from there;
  **config** authored and pushed only from production. They never cross.
- `credentials.yaml` is a binary blob in git — no readable diffs of the secrets
  file (acceptable; secrets rarely change and shouldn't be diffed in the clear).
- A fresh clone must be `git-crypt unlock`ed once before the bot can start, or it
  reads ciphertext and fails.
- Losing the base64 key string makes the backup unrecoverable — the key's
  custody is now the critical dependency.
- On the host, plaintext `credentials.yaml` still exists (the bot needs it);
  git-crypt protects git and the remote, not the running host.
