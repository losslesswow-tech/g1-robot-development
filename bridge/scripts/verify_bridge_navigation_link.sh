#!/usr/bin/env bash
set -euo pipefail

: "${BRIDGE_CONTAINER:=g1-bridge-safe}"
: "${NAVIGATION_CONTAINER:=nav_robot}"
: "${TOPOLOGY:=colocated_jetson}"
: "${REQUIRE_IMU:=false}"
: "${EXPECTED_DDS_INTERFACE:=eth0}"
: "${EXPECTED_DDS_DOMAIN_ID:=0}"
: "${EXPECTED_ROS_MASTER_PORT:=11311}"
: "${ROS_READ_TIMEOUT_SEC:=10}"

fail() {
  echo "ERROR: $*" >&2
  exit 2
}

normalize_bool() {
  case "${1,,}" in
    1|true|yes|on) printf 'true' ;;
    0|false|no|off) printf 'false' ;;
    *) fail "invalid boolean '$1'" ;;
  esac
}

[[ "$ROS_READ_TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]] \
  || fail "ROS_READ_TIMEOUT_SEC must be a positive integer"
[[ "$EXPECTED_ROS_MASTER_PORT" =~ ^[1-9][0-9]*$ ]] \
  || fail "EXPECTED_ROS_MASTER_PORT must be a positive integer"

container_env() {
  docker inspect "$1" --format '{{range .Config.Env}}{{println .}}{{end}}'
}

env_value() {
  local container="$1"
  local name="$2"
  container_env "$container" | sed -n "s/^${name}=//p" | tail -n 1
}

for container in "$BRIDGE_CONTAINER" "$NAVIGATION_CONTAINER"; do
  [[ "$(docker inspect "$container" --format '{{.State.Running}}')" == "true" ]] \
    || fail "container '$container' is not running"
  [[ "$(docker inspect "$container" --format '{{.HostConfig.NetworkMode}}')" == "host" ]] \
    || fail "container '$container' must use host networking"
done

bridge_master="$(env_value "$BRIDGE_CONTAINER" ROS_MASTER_URI)"
navigation_master="$(env_value "$NAVIGATION_CONTAINER" ROS_MASTER_URI)"
bridge_ip="$(env_value "$BRIDGE_CONTAINER" ROS_IP)"
navigation_ip="$(env_value "$NAVIGATION_CONTAINER" ROS_IP)"

[[ -n "$bridge_master" && "$bridge_master" == "$navigation_master" ]] \
  || fail "containers do not use the same ROS_MASTER_URI"
[[ "$(env_value "$BRIDGE_CONTAINER" ROS_DISTRO)" == "noetic" ]] \
  || fail "bridge is not using ROS Noetic"
[[ "$(env_value "$NAVIGATION_CONTAINER" ROS_DISTRO)" == "noetic" ]] \
  || fail "navigation is not using ROS Noetic"
[[ "$(env_value "$BRIDGE_CONTAINER" ENABLE_MOTION)" == "false" ]] \
  || fail "bridge ENABLE_MOTION must be false"
[[ "$(env_value "$BRIDGE_CONTAINER" AUTO_START)" == "false" ]] \
  || fail "bridge AUTO_START must be false"
[[ "$(env_value "$BRIDGE_CONTAINER" MOTION_LOCKED)" == "true" ]] \
  || fail "bridge MOTION_LOCKED must be true"
[[ "$(env_value "$BRIDGE_CONTAINER" DDS_INTERFACE)" == "$EXPECTED_DDS_INTERFACE" ]] \
  || fail "bridge DDS_INTERFACE must be $EXPECTED_DDS_INTERFACE"
[[ "$(env_value "$BRIDGE_CONTAINER" ROS_DOMAIN_ID)" == "$EXPECTED_DDS_DOMAIN_ID" ]] \
  || fail "bridge ROS_DOMAIN_ID must be $EXPECTED_DDS_DOMAIN_ID"

case "$TOPOLOGY" in
  colocated_jetson)
    [[ "$(env_value "$BRIDGE_CONTAINER" DEPLOYMENT_MODE)" == "colocated_jetson" ]] \
      || fail "bridge DEPLOYMENT_MODE is not colocated_jetson"
    [[ "$bridge_ip" == "$navigation_ip" ]] \
      || fail "co-located containers must advertise the same host ROS_IP"
    [[ "$bridge_master" == "http://${bridge_ip}:${EXPECTED_ROS_MASTER_PORT}" ]] \
      || fail "co-located ROS Master must be http://${bridge_ip}:${EXPECTED_ROS_MASTER_PORT}"
    [[ "$(docker image inspect "$(docker inspect "$BRIDGE_CONTAINER" --format '{{.Config.Image}}')" --format '{{.Architecture}}')" == "arm64" ]] \
      || fail "bridge image is not arm64"
    [[ "$(docker image inspect "$(docker inspect "$NAVIGATION_CONTAINER" --format '{{.Config.Image}}')" --format '{{.Architecture}}')" == "arm64" ]] \
      || fail "navigation image is not arm64"
    ;;
  external_navigation)
    [[ "$(env_value "$BRIDGE_CONTAINER" DEPLOYMENT_MODE)" == "external_navigation" ]] \
      || fail "bridge DEPLOYMENT_MODE is not external_navigation"
    [[ "$bridge_ip" != "$navigation_ip" ]] \
      || fail "external topology expects distinct ROS_IP values"
    [[ "$bridge_master" == "http://${navigation_ip}:${EXPECTED_ROS_MASTER_PORT}" ]] \
      || fail "external ROS Master must use the navigation ROS_IP"
    ;;
  *)
    fail "TOPOLOGY must be colocated_jetson or external_navigation"
    ;;
esac

navigation_check='set -euo pipefail
source /opt/ros/noetic/setup.bash
export ROS_MASTER_URI ROS_IP
unset ROS_HOSTNAME || true
status=$(timeout "$ROS_READ_TIMEOUT_SEC" rostopic echo -n 1 /sdk/bridge_status)
case "$status" in
  *read_only_waiting_for_dds_lowstate*|*dds_lowstate_active*) ;;
  *) echo "ERROR: unexpected bridge status: $status" >&2; exit 1 ;;
esac
test "$(rostopic type /sdk/imu)" = sensor_msgs/Imu
node_info=$(timeout "$ROS_READ_TIMEOUT_SEC" rosnode info /g1_ros1_sdk_node)
if grep -Fq /cmd_vel <<<"$node_info"; then
  echo "ERROR: bridge exposes /cmd_vel" >&2
  exit 1
fi'
docker exec -e ROS_READ_TIMEOUT_SEC="$ROS_READ_TIMEOUT_SEC" \
  "$NAVIGATION_CONTAINER" bash -lc "$navigation_check"

if [[ "$(normalize_bool "$REQUIRE_IMU")" == "true" ]]; then
  docker exec -e ROS_READ_TIMEOUT_SEC="$ROS_READ_TIMEOUT_SEC" \
    "$NAVIGATION_CONTAINER" bash -lc \
    'source /opt/ros/noetic/setup.bash; export ROS_MASTER_URI ROS_IP; unset ROS_HOSTNAME || true; timeout "$ROS_READ_TIMEOUT_SEC" rostopic echo -n 1 /sdk/imu >/dev/null'
fi

echo "PASS: navigation can read the safe G1 bridge through ROS1 ($bridge_master)."
