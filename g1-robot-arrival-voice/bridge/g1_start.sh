#!/bin/bash
# G1 桥架启动脚本
source /opt/ros/noetic/setup.bash
source /catkin_ws/devel/setup.bash
export ROS_MASTER_URI=http://127.0.0.1:11311
# 显式指定网卡 enp4s0（覆盖参数服务器可能的残留值）
exec rosrun g1_ros1_sdk g1_ros1_sdk _networkInterface:=enp4s0
