#!/bin/bash
# 诊断：识别导航时那个"手绘模型"到底是哪个话题/哪种 marker
# 用法：机器人在线、触发一次"标记导航"后执行
set -e
source /opt/ros/noetic/setup.bash 2>/dev/null || true
echo "===== /x_nav/nav_data (MarkerArray) ====="
timeout 4 rostopic echo /x_nav/nav_data -n 1 2>/dev/null | grep -E "ns:|id:|type:|action:|text:|position:|scale:" | head -80
echo
echo "===== /x_nav/obs_data (MarkerArray) ====="
timeout 4 rostopic echo /x_nav/obs_data -n 1 2>/dev/null | grep -E "ns:|id:|type:|action:|text:" | head -60
echo
echo "===== /particle_swarm_markers (Marker) ====="
timeout 4 rostopic echo /particle_swarm_markers -n 1 2>/dev/null | grep -E "ns:|id:|type:|action:|scale:|points:" | head -30
echo
echo "===== /x_nav/slam_down_body 是否在发布(机器人身体点云) ====="
timeout 3 rostopic hz /x_nav/slam_down_body 2>/dev/null | tail -1
