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
If the bot fails at startup with a permission error on `data/sensors.db`,
fix the ownership of the host directory:

```bash
docker compose run --rm --user root bot chown -R bot:bot /app/data
```

---

## Useful commands

```bash
docker compose logs -f        # follow logs
docker compose down           # stop
docker compose up -d          # start in background
docker compose restart        # restart
```

## Data

Sensor readings are stored in `./data/sensors.db` (SQLite).
The `data/` folder is mounted as a volume — data persists across restarts.
