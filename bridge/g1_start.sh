#!/usr/bin/env bash
set -euo pipefail

readonly EXPECTED_ARCH="arm64"
: "${ROS_MASTER_URI:=http://192.0.2.6:11311}"
: "${ROS_IP:=192.0.2.8}"
: "${DDS_INTERFACE:=eth0}"
: "${ROS_DOMAIN_ID:=0}"
: "${ENABLE_MOTION:=false}"
: "${AUTO_START:=false}"
: "${MOTION_LOCKED:=true}"
: "${DEPLOYMENT_MODE:=external_navigation}"
: "${REQUIRE_ROS_MASTER:=true}"
: "${ROS_MASTER_CHECK_TIMEOUT_SEC:=3}"

normalize_bool() {
  case "${1,,}" in
    1|true|yes|on) printf 'true' ;;
    0|false|no|off) printf 'false' ;;
    *) echo "Invalid boolean value: $1" >&2; return 2 ;;
  esac
}

enable_motion="$(normalize_bool "$ENABLE_MOTION")"
auto_start="$(normalize_bool "$AUTO_START")"
motion_locked="$(normalize_bool "$MOTION_LOCKED")"
require_ros_master="$(normalize_bool "$REQUIRE_ROS_MASTER")"

command="${1:-run}"
case "$command" in
  run|image-check|network-check) ;;
  *)
    echo "ERROR: unsupported command '$command'; use run, image-check, or network-check." >&2
    exit 2
    ;;
esac

if [[ "$auto_start" == true && "$enable_motion" != true ]]; then
  echo "ERROR: AUTO_START=true requires ENABLE_MOTION=true." >&2
  exit 2
fi
if [[ "$motion_locked" == true && ("$enable_motion" == true || "$auto_start" == true) ]]; then
  echo "ERROR: motion is locked; ENABLE_MOTION and AUTO_START must remain false." >&2
  exit 2
fi
if [[ ! "$ROS_DOMAIN_ID" =~ ^[0-9]+$ ]]; then
  echo "ERROR: ROS_DOMAIN_ID must be a non-negative integer, got '$ROS_DOMAIN_ID'." >&2
  exit 2
fi
if [[ ! "$ROS_MASTER_URI" =~ ^http://([A-Za-z0-9.-]+):([0-9]+)/?$ ]]; then
  echo "ERROR: ROS_MASTER_URI must look like http://host:port, got '$ROS_MASTER_URI'." >&2
  exit 2
fi
master_host="${BASH_REMATCH[1]}"
master_port="${BASH_REMATCH[2]}"
if [[ ! "$ROS_MASTER_CHECK_TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: ROS_MASTER_CHECK_TIMEOUT_SEC must be a positive integer." >&2
  exit 2
fi

case "$DEPLOYMENT_MODE" in
  external_navigation)
    ;;
  colocated_jetson)
    if [[ "$master_host" != "$ROS_IP" ]]; then
      echo "ERROR: colocated_jetson requires the ROS Master host ($master_host) to equal ROS_IP ($ROS_IP)." >&2
      exit 2
    fi
    ;;
  *)
    echo "ERROR: DEPLOYMENT_MODE must be external_navigation or colocated_jetson." >&2
    exit 2
    ;;
esac

/usr/local/bin/verify-architecture "$EXPECTED_ARCH" \
  /opt/unitree_robotics/lib/libunitree_sdk2.a \
  /opt/unitree_robotics/lib/libddsc.so \
  /opt/unitree_robotics/lib/libddscxx.so \
  /opt/g1_bridge/lib/g1_ros1_sdk/g1_ros1_sdk

source /opt/ros/noetic/setup.bash
source /opt/g1_bridge/setup.bash

if [[ "$(rosversion -d)" != "noetic" ]]; then
  echo "ERROR: active ROS distribution is not Noetic." >&2
  exit 2
fi

export ROS_MASTER_URI ROS_IP ROS_DOMAIN_ID
unset ROS_HOSTNAME || true

echo "g1-bridge configuration:"
echo "  DEPLOYMENT_MODE=$DEPLOYMENT_MODE"
echo "  ROS_MASTER_URI=$ROS_MASTER_URI"
echo "  ROS_IP=$ROS_IP"
echo "  DDS_INTERFACE=$DDS_INTERFACE"
echo "  ROS_DOMAIN_ID=$ROS_DOMAIN_ID"
echo "  ENABLE_MOTION=$enable_motion"
echo "  AUTO_START=$auto_start"
echo "  MOTION_LOCKED=$motion_locked"

if [[ "$command" == "image-check" ]]; then
  echo "IMAGE CHECK PASSED: ARM64 artifacts, ROS Noetic, and safety contract are valid."
  exit 0
fi

if [[ ! -d "/sys/class/net/$DDS_INTERFACE" ]]; then
  echo "ERROR: DDS interface '$DDS_INTERFACE' is not visible in the container." >&2
  exit 2
fi
if ! ip -o -4 addr show | awk '{print $4}' | cut -d/ -f1 | grep -Fxq "$ROS_IP"; then
  echo "ERROR: ROS_IP '$ROS_IP' is not assigned to a visible host interface." >&2
  exit 2
fi
if [[ "$require_ros_master" == true ]] \
    && ! timeout "$ROS_MASTER_CHECK_TIMEOUT_SEC" bash -c "</dev/tcp/${master_host}/${master_port}"; then
  echo "ERROR: ROS Master is not reachable at ${master_host}:${master_port}." >&2
  exit 2
fi

if [[ "$command" == "network-check" ]]; then
  echo "NETWORK CHECK PASSED: DDS interface, ROS_IP, and ROS Master are reachable."
  exit 0
fi

if [[ "$enable_motion" == true ]]; then
  echo "WARNING: motion input is explicitly ENABLED; /cmd_vel can reach the G1 motion API." >&2
else
  echo "SAFE MODE: state subscription and ROS status publication only; no motion client or /cmd_vel subscriber."
fi
if [[ "$auto_start" == true ]]; then
  echo "WARNING: AUTO_START is explicitly ENABLED; startup will call StandUp, wait, then Start." >&2
else
  echo "AUTO_START is disabled; container start/restart will not call StandUp or Start."
fi

exec rosrun g1_ros1_sdk g1_ros1_sdk \
  _network_interface:="$DDS_INTERFACE" \
  _dds_domain_id:="$ROS_DOMAIN_ID" \
  _enable_motion:="$enable_motion" \
  _auto_start:="$auto_start"
