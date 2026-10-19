cmake_minimum_required(VERSION 3.0.2)
project(g1_ros1_sdk)

set(CMAKE_CXX_STANDARD 17)

find_package(catkin REQUIRED COMPONENTS
  roscpp
  rospy
  std_msgs
  sensor_msgs
  geometry_msgs
  nav_msgs
)
find_package(Threads REQUIRED)

# 头文件搜索路径（与 go2_ros1_sdk 一致 + /usr/local/include 提供 dds/ddsrt）
# 顺序很重要：ddsc, ddscxx 在前；/usr/local/include 提供 dds/ddsrt 和 dds/ddsi
include_directories(/usr/local/include/ddsc)
include_directories(/usr/local/include/ddscxx)
include_directories(/usr/local/include)

catkin_package(CATKIN_DEPENDS roscpp rospy std_msgs sensor_msgs geometry_msgs nav_msgs)

include_directories(${catkin_INCLUDE_DIRS})

add_executable(g1_ros1_sdk src/g1_ros1_sdk.cpp)
target_link_libraries(g1_ros1_sdk
  ${catkin_LIBRARIES}
  unitree_sdk2
  ddsc
  ddscxx
  fmt
  rt
  pthread
)

add_executable(g1_control src/g1_control.cpp)
target_link_libraries(g1_control
  ${catkin_LIBRARIES}
  unitree_sdk2
  ddsc
  ddscxx
  fmt
  rt
  pthread
)

add_executable(g1_arm src/g1_arm.cpp)
target_link_libraries(g1_arm
  ${catkin_LIBRARIES}
  unitree_sdk2
  ddsc
  ddscxx
  fmt
  rt
  pthread
)
