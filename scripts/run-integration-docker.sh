#!/usr/bin/env bash
# The one command (ticket #9) to get a real, out-of-the-box, working
# integration-test environment: builds the Docker image (which installs
# the headless SC2 client + map pool via `sc2-sdk-setup` inside the
# container, see ../Dockerfile) and runs the real integration suite
# against it. No host SC2 install, Battle.net, or manual setup required --
# only Docker.
#
# Usage: ./scripts/run-integration-docker.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

docker build --platform linux/amd64 -t sc2-sdk-integration .
docker run --rm --platform linux/amd64 sc2-sdk-integration
