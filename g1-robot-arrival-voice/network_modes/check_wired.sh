#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# Read-only checks preserve the existing wired bridge and all network rules.
python3 -c 'import sys; sys.path.insert(0,"../wifi_access"); import network_probe as n; from session import wired_ready; wired_ready(n.inventory()); print(n.ensure_idle())'
docker exec nav_robot timeout 25 /opt/g1-arrival-voice/network-modes-20261010/wifi_access/build/g1_wifi_readonly --wired
docker exec nav_robot bash -c 'source /catkin_ws/devel/setup.bash && export ROS_MASTER_URI=http://192.0.2.5:11311 ROS_IP=192.0.2.5 && timeout 18 python3 /opt/g1-arrival-voice/network-modes-20261010/wifi_access/sensor_probe.py'
