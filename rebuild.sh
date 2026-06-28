#!/bin/bash
set -e

# Rebuild the image from the LOCAL tree and restart — no git pull.
# Use after editing files locally on the host (deploy.sh pulls from git first).

# target the rootless Docker daemon
#export DOCKER_HOST="unix://${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/docker.sock"
docker context use rootless

docker compose down
docker compose build --no-cache
docker compose up -d
echo "to see log:  docker compose logs -f"
