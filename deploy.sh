#!/bin/bash
set -e

# always target the rootless Docker daemon (DOCKER_HOST wins over CLI context)
#export DOCKER_HOST="unix://${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/docker.sock"
docker context use rootless

if [ -d .git ]; then
    git pull
else
    git clone git@github.com:ghedolo/mqtt-telegram-bot.git .
fi

docker compose down
docker compose build --no-cache --pull
docker compose up -d
echo "to see log:  docker compose logs -f"
