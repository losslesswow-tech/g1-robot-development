#!/usr/bin/env bash
set -euo pipefail

readonly EXPECTED_ROS_DISTRO="noetic"
readonly EXPECTED_MASTER_URI="http://192.0.2.6:11311"
readonly EXPECTED_ROS_IP="192.0.2.6"
readonly EXPECTED_DDS_INTERFACE="eth0"
readonly EXPECTED_DDS_DOMAIN="0"

: "${ROS_DISTRO:=noetic}"
: "${ROS_MASTER_URI:=$EXPECTED_MASTER_URI}"
: "${ROS_IP:=$EXPECTED_ROS_IP}"
: "${DDS_INTERFACE:=$EXPECTED_DDS_INTERFACE}"
: "${ROS_DOMAIN_ID:=$EXPECTED_DDS_DOMAIN}"
: "${ENABLE_MOTION:=false}"
: "${AUTO_START:=false}"
: "${NAVIGATION_AUTO_START:=false}"
: "${ALLOW_CMD_VEL:=false}"
: "${REQUIRE_LOCAL_ROS_IP:=true}"
: "${X_NAV_CONFIG:=/nav_map/x_nav.yaml}"

fail() {
  echo "ERROR: $*" >&2
  exit 2
}

normalize_bool() {
  case "${1,,}" in
    1|true|yes|on) printf 'true' ;;
    0|false|no|off) printf 'false' ;;
    *) fail "invalid boolean value '$1'" ;;
  esac
}

require_false() {
  local name="$1"
  local value
  value="$(normalize_bool "${!name}")"
  [[ "$value" == "false" ]] || fail "$name must remain false in the G1 navigation consumer"
}

static_check() {
  [[ "$(dpkg --print-architecture)" == "amd64" ]] || fail "navigation image must be linux/amd64"
  [[ "$(uname -m)" != "aarch64" ]] \
    || fail "this amd64 navigation image must not run under emulation on the Jetson; build a genuine linux/arm64 image"
  [[ "$ROS_DISTRO" == "$EXPECTED_ROS_DISTRO" ]] || fail "ROS_DISTRO must be noetic"
  [[ "$ROS_MASTER_URI" == "$EXPECTED_MASTER_URI" ]] || fail "ROS_MASTER_URI must be $EXPECTED_MASTER_URI"
  [[ "$ROS_IP" == "$EXPECTED_ROS_IP" ]] || fail "ROS_IP must be $EXPECTED_ROS_IP"
  [[ "$DDS_INTERFACE" == "$EXPECTED_DDS_INTERFACE" ]] || fail "bridge contract requires DDS_INTERFACE=eth0"
  [[ "$ROS_DOMAIN_ID" == "$EXPECTED_DDS_DOMAIN" ]] || fail "bridge contract requires ROS_DOMAIN_ID=0"
  require_false ENABLE_MOTION
  require_false AUTO_START
  require_false NAVIGATION_AUTO_START
  require_false ALLOW_CMD_VEL

  [[ -r /opt/ros/noetic/setup.bash ]] || fail "ROS Noetic setup is missing"
  [[ -r /catkin_ws/devel/setup.bash ]] || fail "catkin workspace setup is missing"
  [[ -r "$X_NAV_CONFIG" ]] || fail "safe x_nav configuration is missing: $X_NAV_CONFIG"

  if env | grep -Eq '^(AMENT_PREFIX_PATH|COLCON_PREFIX_PATH)='; then
    fail "ROS 2 environment detected; Noetic must not be mixed with Foxy/ament/colcon"
  fi
  if [[ "${CMAKE_PREFIX_PATH:-}" == *"/opt/ros/foxy"* ]] || [[ "${PYTHONPATH:-}" == *"/opt/ros/foxy"* ]]; then
    fail "Foxy paths detected in the Noetic navigation environment"
  fi
}

load_noetic() {
  unset ROS_HOSTNAME AMENT_PREFIX_PATH COLCON_PREFIX_PATH || true
  # shellcheck disable=SC1091
  source /opt/ros/noetic/setup.bash
  # shellcheck disable=SC1091
  source /catkin_ws/devel/setup.bash
  export ROS_MASTER_URI ROS_IP ROS_DISTRO
}

network_preflight() {
  local require_local_ip
  local local_ips
  local master_host
  local master_port

  require_local_ip="$(normalize_bool "$REQUIRE_LOCAL_ROS_IP")"
  if command -v ip >/dev/null 2>&1; then
    local_ips="$(ip -o -4 addr show | awk '{print $4}' | cut -d/ -f1)"
  elif command -v hostname >/dev/null 2>&1; then
    local_ips="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+(\.[0-9]+){3}$' || true)"
  else
    fail "cannot enumerate local IPv4 addresses (neither ip nor hostname is available)"
  fi
  if [[ "$require_local_ip" == "true" ]] \
      && ! grep -Fxq "$ROS_IP" <<<"$local_ips"; then
    fail "ROS_IP $ROS_IP is not assigned inside this network namespace (visible: ${local_ips//$'\n'/, }); full ROS1 XMLRPC/TCPROS callbacks would be unreliable"
  fi

  if [[ ! "$ROS_MASTER_URI" =~ ^http://([A-Za-z0-9.-]+):([0-9]+)/?$ ]]; then
    fail "invalid ROS_MASTER_URI '$ROS_MASTER_URI'"
  fi
  master_host="${BASH_REMATCH[1]}"
  master_port="${BASH_REMATCH[2]}"
  timeout 3 bash -c "</dev/tcp/${master_host}/${master_port}" \
    || fail "ROS Master is not reachable at ${master_host}:${master_port}"
}

print_contract() {
  cat <<EOF
G1 navigation consumer contract:
  ROS_DISTRO=$ROS_DISTRO
  ROS_MASTER_URI=$ROS_MASTER_URI
  ROS_IP=$ROS_IP
  bridge DDS_INTERFACE=$DDS_INTERFACE
  bridge ROS_DOMAIN_ID=$ROS_DOMAIN_ID
  ENABLE_MOTION=$ENABLE_MOTION
  AUTO_START=$AUTO_START
  NAVIGATION_AUTO_START=$NAVIGATION_AUTO_START
  ALLOW_CMD_VEL=$ALLOW_CMD_VEL
  X_NAV_CONFIG=$X_NAV_CONFIG
EOF
}

static_check
load_noetic

command="${1:-hold}"
case "$command" in
  image-check)
    print_contract
    rosversion -d | grep -Fxq noetic || fail "active ROS environment is not Noetic"
    rospack find x_nav >/dev/null
    rospack find x_nav_control >/dev/null
    /usr/local/bin/validate-g1-navigation-overlay
    echo "IMAGE CHECK PASSED: Noetic navigation files are present and all motion/autostart guards are false."
    ;;
  preflight)
    network_preflight
    print_contract
    echo "NETWORK PREFLIGHT PASSED: ROS_IP is locally owned and ROS Master is reachable."
    ;;
  observe)
    network_preflight
    print_contract
    echo "ROS nodes:"
    rosnode list
    echo "G1 bridge topics:"
    rostopic list | grep '^/sdk/' || true
    echo "Bridge status (up to 5 seconds):"
    timeout 5 rostopic echo -n 1 /sdk/bridge_status || true
    ;;
  hold)
    print_contract
    echo "SAFE HOLD: navigation, SLAM, planner, sensor drivers, and robot control are not auto-started."
    exec sleep infinity
    ;;
  *)
    fail "unsupported command '$command'; use image-check, preflight, observe, or hold"
    ;;
esac
