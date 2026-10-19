# 基于原 bridge 镜像改造：新增 G1 ROS1 控制节点
FROM registry.cn-guangzhou.aliyuncs.com/z_nav/bridge:1.0

# 1. 安装 unitree_sdk2 库（从导航容器提取的）
COPY unitree_sdk2/include /usr/local/include/unitree
COPY unitree_sdk2/include_ddsc /usr/local/include/ddsc
COPY unitree_sdk2/include_dds /usr/local/include/dds
COPY unitree_sdk2/include_ddscxx /usr/local/include/ddscxx
COPY unitree_sdk2/libunitree_sdk2.a /usr/local/lib/
COPY unitree_sdk2/libddsc.so /usr/local/lib/
COPY unitree_sdk2/libddscxx.so /usr/local/lib/
RUN ldconfig

# 2. 编译 g1_ros1_sdk ROS 节点
COPY pkg /catkin_ws/src/g1_ros1_sdk
RUN bash -c "source /opt/ros/noetic/setup.bash && cd /catkin_ws && catkin_make -DCATKIN_WHITELIST_PACKAGES=g1_ros1_sdk"

# 3. 启动脚本：跑 g1_ros1_sdk（替代原来的 M20 桥接）
COPY g1_start.sh /g1_start.sh
RUN chmod +x /g1_start.sh

CMD ["/ros_entrypoint.sh", "/bin/bash", "/g1_start.sh"]
