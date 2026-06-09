#!/bin/bash
set -e

if [ -d .git ]; then
    git pull
else
    git clone git@github.com:ghedalo/mqtt-telegram-bot.git .
fi

docker compose down
docker compose build --no-cache
docker compose up -d
