#!/usr/bin/env bash
# Start u1sim + Snapmaker's real Moonraker fork on localhost:7125 for integration tests.
# Needs Docker Compose >= 2.20 (docker-compose.test.yml uses `include:`).
set -euo pipefail
cd "$(dirname "$0")/.."

# Clone $1 into $2 at commit $3, submodules included. An empty directory (an
# interrupted clone, or a submodule that was never initialised) counts as missing.
fetch() {
  local url=$1 dir=$2 sha=$3
  if [ -d "$dir" ] && [ -z "$(ls -A "$dir")" ]; then
    rmdir "$dir"
  fi
  if [ ! -d "$dir" ]; then
    git clone --recurse-submodules "$url" "$dir"
    git -C "$dir" checkout --quiet "$sha"
  fi
  git -C "$dir" submodule update --init --recursive
}

mkdir -p vendor
fetch https://github.com/zkasuran/u1-companion vendor/u1-companion 69de50e
fetch https://github.com/Snapmaker/U1-Moonraker vendor/u1-companion/vendor/u1-moonraker 3b8357c

# idle_loaded: standby, all four heads loaded and identified, nothing printing.
# u1sim's default scenario loops a print of its own and empties the heads between laps.
export U1SIM_SCENARIO="${U1SIM_SCENARIO:-idle_loaded}"
# Moonraker's pinned streaming-form-data==1.15.0 has x86 wheels only, and the image
# has no compiler, so arm64 hosts (Apple Silicon) build and run the pair emulated.
export DOCKER_DEFAULT_PLATFORM="${DOCKER_DEFAULT_PLATFORM:-linux/amd64}"
docker compose -f docker-compose.test.yml up -d --build --wait
# The fork checks upload space on the U1's /userdata partition (file_manager.py
# get_user_space), which this stack lacks; without it every upload is HTTP 413.
docker compose -f docker-compose.test.yml exec -T moonraker mkdir -p /userdata
curl -s --retry 30 --retry-connrefused --retry-delay 1 http://localhost:7125/server/info > /dev/null
echo "u1sim is up on http://localhost:7125"
