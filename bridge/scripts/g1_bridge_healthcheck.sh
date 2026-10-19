#!/usr/bin/env bash
set -euo pipefail

# shellcheck disable=SC1091
source /opt/ros/noetic/setup.bash
# shellcheck disable=SC1091
source /opt/g1_bridge/setup.bash

export ROS_MASTER_URI ROS_IP
unset ROS_HOSTNAME || true

status_output="$(timeout 5 rostopic echo -n 1 /sdk/bridge_status)"
grep -Eq 'data: "?(read_only_waiting_for_dds_lowstate|dds_lowstate_active)"?' \
  <<<"$status_output"

# A safe bridge must not register a /cmd_vel subscription.
node_info="$(timeout 5 rosnode info /g1_ros1_sdk_node)"
if grep -Fq '/cmd_vel' <<<"$node_info"; then
  echo "ERROR: safe bridge unexpectedly exposes /cmd_vel." >&2
  exit 1
fi
