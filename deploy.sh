#!/bin/bash
set -e

# always target the rootless Docker daemon (DOCKER_HOST wins over CLI context)
export DOCKER_HOST="unix://${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/docker.sock"

if [ -d .git ]; then
    git pull
else
    git clone git@github.com:ghedolo/mqtt-telegram-bot.git .
fi

docker compose down
docker compose build --no-cache
docker compose up -d
