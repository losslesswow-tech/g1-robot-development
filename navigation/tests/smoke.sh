#!/usr/bin/env bash
set -euo pipefail

readonly compose_file="${1:-docker-compose.yml}"
readonly image="g1-navigation:noetic-safe"

docker compose -f "$compose_file" config >/dev/null
docker compose -f "$compose_file" build --pull=false
docker compose -f "$compose_file" run --rm navigation image-check

expect_guard_rejection() {
  local variable="$1"
  local guard_log
  guard_log="$(mktemp)"
  if docker run --rm --network none \
      --env "${variable}=true" \
      "$image" image-check >"$guard_log" 2>&1; then
    echo "ERROR: $variable=true was not rejected" >&2
    cat "$guard_log" >&2
    rm -f "$guard_log"
    return 1
  fi
  grep -Fq "$variable must remain false" "$guard_log"
  rm -f "$guard_log"
  echo "guard passed: $variable=true rejected"
}

expect_guard_rejection ENABLE_MOTION
expect_guard_rejection AUTO_START
expect_guard_rejection NAVIGATION_AUTO_START
expect_guard_rejection ALLOW_CMD_VEL

echo "G1 navigation smoke tests passed."
