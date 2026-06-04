# LorTemp Bot — Install on Debian

## Prerequisites

Docker installed on the target machine.

---

## Option A — Standard Docker (runs as root)

```bash
unzip lortebot-deploy.zip -d lortebot
cd lortebot
docker compose up --build -d
```

---

## Option B — Rootless Docker (recommended)

Rootless runs the Docker daemon as a normal user.
If a container is compromised, it has no root access to the host.

### Setup (once)

```bash
# dependencies
sudo apt install -y uidmap dbus-user-session

# install rootless daemon for current user
dockerd-rootless-setuptool.sh install

# add to ~/.bashrc or ~/.zshrc
export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/docker.sock

# reload shell, then enable and start
systemctl --user enable docker
systemctl --user start docker
```

### Run

```bash
unzip lortebot-deploy.zip -d lortebot
cd lortebot
docker compose up --build -d
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
