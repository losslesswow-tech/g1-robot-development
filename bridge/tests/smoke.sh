#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-g1-bridge:arm64}"
KEEP_FAILED_CONTAINERS="${KEEP_FAILED_CONTAINERS:-false}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

expect_rejected() {
  local label="$1"
  shift
  if "$@"; then
    fail "$label was accepted"
  fi
  echo "PASS: $label rejected"
}

[[ "$(docker image inspect "$IMAGE" --format '{{.Architecture}}')" == "arm64" ]] \
  || fail "$IMAGE is not arm64"

docker run --rm --platform linux/arm64 --network none \
  -e DEPLOYMENT_MODE=external_navigation \
  -e ROS_MASTER_URI=http://192.0.2.6:11311 \
  -e ROS_IP=192.0.2.8 \
  -e DDS_INTERFACE=eth0 \
  -e ENABLE_MOTION=false \
  -e AUTO_START=false \
  "$IMAGE" image-check

docker run --rm --platform linux/arm64 --network none \
  -e DEPLOYMENT_MODE=colocated_jetson \
  -e ROS_MASTER_URI=http://192.0.2.8:11311 \
  -e ROS_IP=192.0.2.8 \
  -e DDS_INTERFACE=eth0 \
  -e ENABLE_MOTION=false \
  -e AUTO_START=false \
  "$IMAGE" image-check

expect_rejected "ENABLE_MOTION=true while motion lock is active" \
  docker run --rm --platform linux/arm64 --network none \
    -e ENABLE_MOTION=true "$IMAGE" image-check
expect_rejected "AUTO_START=true while motion lock is active" \
  docker run --rm --platform linux/arm64 --network none \
    -e AUTO_START=true "$IMAGE" image-check
expect_rejected "co-located ROS Master/ROS_IP mismatch" \
  docker run --rm --platform linux/arm64 --network none \
    -e DEPLOYMENT_MODE=colocated_jetson \
    -e ROS_MASTER_URI=http://192.0.2.6:11311 \
    -e ROS_IP=192.0.2.8 \
    "$IMAGE" image-check

docker compose -f "$BRIDGE_DIR/docker-compose.yml" config >/dev/null
docker compose \
  -f "$BRIDGE_DIR/docker-compose.yml" \
  -f "$BRIDGE_DIR/docker-compose.colocated.yml" \
  config >/dev/null

# Offline ROS1 integration harness. All containers share the local Docker host
# network. The bridge remains read-only: it only subscribes to lowstate and does
# not create a motion client or any actuator-capable ROS subscription.
suffix="$$"
master_container="g1-bridge-smoke-master-${suffix}"
bridge_container="g1-bridge-smoke-bridge-${suffix}"
navigation_container="g1-bridge-smoke-navigation-${suffix}"
master_port="$((21000 + suffix % 10000))"
master_uri="http://127.0.0.1:${master_port}"

cleanup() {
  local result="$?"
  if [[ "$result" -ne 0 ]]; then
    echo "--- offline ROS Master log ---" >&2
    docker logs "$master_container" >&2 2>/dev/null || true
    echo "--- offline bridge log ---" >&2
    docker logs "$bridge_container" >&2 2>/dev/null || true
    echo "--- offline ROS graph from observer ---" >&2
    docker exec "$navigation_container" bash -lc \
      'source /opt/ros/noetic/setup.bash; rosnode list; rostopic list; rosnode info /g1_ros1_sdk_node; rostopic info /sdk/bridge_status; timeout 10 rostopic echo -n 1 /sdk/bridge_status' \
      >&2 || true
    if [[ "$KEEP_FAILED_CONTAINERS" == "true" ]]; then
      echo "Keeping failed containers for inspection:" >&2
      echo "  $master_container" >&2
      echo "  $bridge_container" >&2
      echo "  $navigation_container" >&2
      return
    fi
  fi
  docker stop \
    "$bridge_container" "$navigation_container" "$master_container" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d --rm --platform linux/arm64 --network host \
  --name "$master_container" --entrypoint /bin/bash \
  -e ROS_MASTER_URI="$master_uri" -e ROS_IP=127.0.0.1 \
  "$IMAGE" -lc "source /opt/ros/noetic/setup.bash; exec roscore -p $master_port" \
  >/dev/null

master_ready=false
for _ in $(seq 1 20); do
  if docker exec "$master_container" \
      bash -lc "</dev/tcp/127.0.0.1/$master_port" 2>/dev/null; then
    master_ready=true
    break
  fi
  sleep 1
done
[[ "$master_ready" == true ]] || fail "offline ROS Master did not start"

docker run -d --rm --platform linux/arm64 --network host \
  --name "$navigation_container" --entrypoint /bin/bash \
  -e ROS_MASTER_URI="$master_uri" -e ROS_IP=127.0.0.1 \
  -e ROS_DISTRO=noetic \
  "$IMAGE" -lc 'exec sleep infinity' >/dev/null

docker run -d --rm --platform linux/arm64 --network host \
  --name "$bridge_container" \
  -e ROS_MASTER_URI="$master_uri" -e ROS_IP=127.0.0.1 \
  -e DDS_INTERFACE=eth0 -e ROS_DOMAIN_ID=0 \
  -e ENABLE_MOTION=false -e AUTO_START=false -e MOTION_LOCKED=true \
  -e DEPLOYMENT_MODE=colocated_jetson \
  "$IMAGE" >/dev/null

bridge_ready=false
for _ in $(seq 1 20); do
  [[ "$(docker inspect "$bridge_container" --format '{{.State.Running}}')" == "true" ]] \
    || { docker logs "$bridge_container"; fail "offline bridge stopped"; }
  if docker exec "$navigation_container" bash -lc \
      'source /opt/ros/noetic/setup.bash; timeout 10 rostopic echo -n 1 /sdk/bridge_status >/dev/null' \
      2>/dev/null; then
    bridge_ready=true
    break
  fi
  sleep 1
done
[[ "$bridge_ready" == true ]] || fail "navigation observer could not read bridge status"

BRIDGE_CONTAINER="$bridge_container" \
NAVIGATION_CONTAINER="$navigation_container" \
TOPOLOGY=colocated_jetson \
REQUIRE_IMU=false \
EXPECTED_DDS_INTERFACE=eth0 \
EXPECTED_ROS_MASTER_PORT="$master_port" \
bash "$BRIDGE_DIR/scripts/verify_bridge_navigation_link.sh"

echo "PASS: offline ARM64 bridge/navigation ROS1 smoke test completed without motion interfaces."
