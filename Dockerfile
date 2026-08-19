# Self-contained integration-test environment for sc2-sdk (ticket #9).
#
# Builds an image that installs Blizzard's official headless Linux SC2
# client + this project's fixed map pool *inside the container* (via
# `sc2-sdk-setup`, ticket #2 -- see src/install/), so `pytest -m integration`
# boots a real game with no host Linux machine, Battle.net, or manual setup
# required.
#
# The headless package Blizzard publishes (see src/install/headless.py) is a
# native x86_64 Linux binary -- there is no arm64 build. This image is
# therefore pinned to linux/amd64 explicitly: on an x86_64 Docker host (e.g.
# GitHub Actions' standard Linux runners) that's a no-op; on an arm64 host
# (e.g. Apple Silicon via Colima/Docker Desktop) it runs under QEMU
# user-mode emulation, which works for `docker build` but has not been
# confirmed to run the actual SC2 client correctly -- see README.md's
# "Real-game integration tests in Docker" section for exactly what is and
# isn't verified.
FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

# `unzip` gives install.headless a C-accelerated PKWARE/ZipCrypto decrypt
# path for the ~4GB headless package -- orders of magnitude faster than
# falling back to Python's stdlib `zipfile` (see src/install/headless.py).
# Without this, sc2-sdk-setup below silently falls back to the slow path.
RUN apt-get update && apt-get install -y --no-install-recommends unzip \
    && rm -rf /var/lib/apt/lists/*

# Install the project (editable, with dev/test deps) before copying the
# rest of the source so dependency layers cache across source-only edits.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[dev]"

COPY tests ./tests

# Installs the headless SC2 client + fixed map pool (install.maps.DEFAULT_MAPS)
# to the default Linux location (~/StarCraftII), baked into the image so the
# integration suite needs no network access at test/container-run time.
# This is the same `sc2-sdk-setup` a human runs on a bare Linux host --
# nothing Docker-specific about how the client gets installed.
RUN sc2-sdk-setup

CMD ["pytest", "-m", "integration", "-v"]
