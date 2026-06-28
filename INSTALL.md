# LorTemp Bot — Install on Debian

## Prerequisites

Docker installed on the target machine, running **rootless** (see setup below).
Rootless runs the Docker daemon as a normal user: if a container is
compromised, the attacker has no root access to the host. It is the only
supported deployment mode.

---

## Rootless Docker setup (once)

```bash
# if rootful Docker is running, disable it first
# (this stops any containers managed by the rootful daemon)
sudo systemctl disable --now docker.service docker.socket
sudo systemctl stop docker.socket docker.service

# remove the stale socket file if left behind (the setup tool
# aborts if /var/run/docker.sock exists, even with no daemon)
sudo rm -f /var/run/docker.sock

# dependencies
sudo apt install -y uidmap dbus-user-session

# install rootless daemon for current user
dockerd-rootless-setuptool.sh install

# add to ~/.bashrc or ~/.zshrc
export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/docker.sock

# reload shell, then enable and start
systemctl --user enable docker
systemctl --user start docker

# keep the user services running after logout
sudo loginctl enable-linger $USER
```

### Re-enable rootful Docker (optional)

If other containers on the host run under the rootful daemon, turn it
back on after the rootless setup is complete — the two daemons coexist:

```bash
sudo systemctl enable --now docker.service docker.socket
```

### Selecting the daemon: DOCKER_HOST vs context

Two independent mechanisms select which daemon `docker` talks to, and
`DOCKER_HOST` **always wins** over the CLI context:

- `DOCKER_HOST=unix://$XDG_RUNTIME_DIR/docker.sock` → rootless daemon
- `docker context use rootless` / `default` → ignored if `DOCKER_HOST` is set

Recommended setup — use only `DOCKER_HOST` (in `~/.bashrc`):

- every new shell talks to the **rootless** daemon → bot management
- to manage rootful containers: `unset DOCKER_HOST` in that shell first,
  and check with `docker info | grep -i rootless` (must print nothing)

**Pitfall:** if you run `docker compose up` for the rootful containers
while `DOCKER_HOST` still points to rootless, the containers are
*created in the rootless daemon* and fail with errors like
`RootlessKit PortManager ... cannot expose privileged port 80` or
`chown: Operation not permitted` on volumes. Fix: with `DOCKER_HOST`
still set (or `docker context use rootless`), run `docker compose down`
in that project to remove the misplaced containers; then `unset
DOCKER_HOST`, verify with `docker info`, and `docker compose up -d`
again on the rootful daemon.

---

## Run

```bash
unzip lortebot-deploy.zip -d lortebot
cd lortebot
docker compose up --build -d
```

The container runs as a non-root user with a read-only filesystem,
all capabilities dropped, and memory/CPU/pid limits
(see `docker-compose.yml`).

### Data directory permissions

The container writes SQLite data to `./data` as an unprivileged user.
If the bot fails at startup with a permission error on `data/sensors.db`
(typical when the directory was created by a previous rootful
deployment and is owned by root), fix the ownership in two steps:

```bash
# 1. on the host: chown to your user (maps to container root in rootless)
sudo chown -R $USER:$USER ./data

# 2. from a container: chown to the bot user (plain docker run —
#    `docker compose run` won't work here because cap_drop blocks chown)
docker run --rm --user root -v "$PWD/data:/data" lortebot-bot chown -R bot:bot /data
```

(adjust the image name if different — check `docker images`)

After the fix, the host shows a high uid (e.g. `100998`) on the files:
that is the rootless subuid mapped to the container's `bot` user —
correct, leave it. The `bot` uid is pinned to 999 in the Dockerfile, so
image rebuilds can never shift the mapping and break the volume
permissions again. This is a one-time fix per host.

---

## Useful commands

```bash
docker compose logs -f        # follow logs
docker compose down           # stop
docker compose up -d          # start in background
docker compose restart        # restart
```

Two helper scripts (both target the rootless daemon via `docker context use rootless`):

```bash
./deploy.sh     # git pull + rebuild (--no-cache) + restart — pull remote changes
./rebuild.sh    # rebuild (--no-cache) + restart, NO git pull — apply local host edits
```

## Data

Sensor readings are stored in `./data/sensors.db` (SQLite).
The `data/` folder is mounted as a volume — data persists across restarts.
