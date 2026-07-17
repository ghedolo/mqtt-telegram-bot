# Backing up `sensors.d/` and `credentials.yaml`

The bot's configuration — `sensors.d/` (device topology) and `credentials.yaml`
(Telegram token, MQTT password, user chat_ids) — is **gitignored by the code
repo** and lives only on the production host. If the host dies, it is lost.

This document sets up a **separate, private, encrypted backup repo**, operated
**only from production**. The development Mac never holds the secrets.

## Design

- A private GitHub repo `lortebot-config`, a **sibling** of the deploy dir `bot/`.
- `credentials.yaml` is encrypted at rest with [git-crypt](https://github.com/AGWA/git-crypt);
  `sensors.d/` stays plaintext (it holds no secrets, only group *names*), so its
  history stays diff-able.
- Encryption is **transparent**: the working tree is plaintext (what the bot
  reads via bind mount), only the git blobs and the remote hold ciphertext.
- The git-crypt **symmetric key** is exported as a base64 text string and kept
  in a password manager — the single artifact that, with the repo, decrypts
  everything. It must survive the host dying.

```
<parent>/
├── bot/                  ← deploy dir (code .git, data/ stay here)
│   ├── docker-compose.yml
│   ├── .env              ← LORTE_CONFIG=../lortebot-config  (prod only, gitignored)
│   └── data/
└── lortebot-config/      ← this backup repo (its own .git; the git-crypt key lives here)
    ├── .gitattributes    ← credentials.yaml filter=git-crypt diff=git-crypt
    ├── sensors.d/        ← plaintext
    └── credentials.yaml  ← git-crypt encrypted
```

Only the **subpaths** `sensors.d`/`credentials.yaml` are bind-mounted into the
container, so `.git/` — and the git-crypt key inside it — never enters the container.

## Setup (once, on production)

`<parent>` is the directory that contains `bot/`.

```bash
sudo apt install -y git-crypt

# 1. Create an EMPTY private repo on GitHub: lortebot-config

# 2. Build the config repo from the files already on the host.
#    They are gitignored by the code repo, so `mv` is safe.
cd <parent>
mkdir lortebot-config && cd lortebot-config
git init && git branch -M main
git remote add origin git@github.com:<you>/lortebot-config.git
mv ../bot/sensors.d ./
mv ../bot/credentials.yaml ./
printf 'credentials.yaml filter=git-crypt diff=git-crypt\n' > .gitattributes
git-crypt init
git add -A && git commit -m "config: initial (credentials.yaml encrypted)"
git push -u origin main

# 3. Export the key as a text string and store it in your password manager.
git-crypt export-key /dev/stdout | base64 | tr -d '\n' ; echo
```

Then point the deployment at the sibling and restart:

```bash
cd <parent>/bot
git pull                                  # pulls the updated docker-compose.yml
echo "LORTE_CONFIG=../lortebot-config" >> .env
./rebuild.sh                              # remounts config from the new path
```

`docker-compose.yml` mounts `${LORTE_CONFIG:-.}/sensors.d` and
`.../credentials.yaml`. The default `.` keeps the old in-place layout; setting
`LORTE_CONFIG` in `bot/.env` moves the source to the sibling repo.

## Daily use (backing up a config change)

After editing `sensors.d/` or `credentials.yaml`:

```bash
cd <parent>/lortebot-config
git add -A && git commit -m "..." && git push
```

Then apply it to the running bot: `/reloadConfig` (or restart if you added a
topic or a Signal, which need a new MQTT subscription).

Optional: a `config-sync.sh` + cron for an automatic daily commit/push.

## Restore (on a fresh host)

```bash
sudo apt install -y git-crypt
cd <parent>
git clone git@github.com:<you>/lortebot-config.git lortebot-config

# Decode the key from the password manager and unlock — ONCE per host.
echo "THE_BASE64_STRING" | base64 -d > /tmp/gc.key
cd lortebot-config && git-crypt unlock /tmp/gc.key && shred -u /tmp/gc.key
# credentials.yaml is now plaintext in the working tree.

# Then bring up the code deploy as usual:
cd <parent> && git clone git@github.com:ghedolo/mqtt-telegram-bot.git bot
cd bot && echo "LORTE_CONFIG=../lortebot-config" >> .env && ./deploy.sh
```

## Security notes

- On production, plaintext `credentials.yaml` **exists anyway** — the bot reads
  it. git-crypt protects the files **in git and on the remote**, not on the host.
  The threat model is a backup/remote leak, not root on the host.
- After `git-crypt unlock`, the working tree stays plaintext across later
  `git pull`s (git-crypt keeps the key in `.git/`). A clone that was **not**
  unlocked shows `credentials.yaml` as ciphertext, and the bot would fail to
  start — always unlock once after cloning.
- The base64 string in the password manager is the only point of recovery: lose
  it and the backup is unrecoverable. Keep a second offline copy.

## Verifying the encryption

```bash
# stored blob starts with the git-crypt magic, not YAML:
git -C <parent>/lortebot-config show HEAD:credentials.yaml | head -c 16 | xxd
# sensors.d is readable plaintext:
git -C <parent>/lortebot-config show HEAD:sensors.d/00-defaults.yaml
```

On GitHub the `credentials.yaml` blob shows as binary/encrypted; `sensors.d/`
files show as normal text.
