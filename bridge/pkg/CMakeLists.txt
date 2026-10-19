cmake_minimum_required(VERSION 3.10)
project(g1_ros1_sdk)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(BUILD_CONTROL_TOOLS "Build actuator-capable g1_control and g1_arm utilities" OFF)

find_package(catkin REQUIRED COMPONENTS
  geometry_msgs
  nav_msgs
  roscpp
  rospy
  sensor_msgs
  std_msgs
)
find_package(fmt REQUIRED)
find_package(Threads REQUIRED)
find_package(unitree_sdk2 REQUIRED CONFIG)

catkin_package(
  CATKIN_DEPENDS geometry_msgs nav_msgs roscpp rospy sensor_msgs std_msgs
)

include_directories(${catkin_INCLUDE_DIRS})

function(configure_g1_target target)
  target_link_libraries(${target}
    ${catkin_LIBRARIES}
    unitree_sdk2
    fmt::fmt
    Threads::Threads
    rt
  )
  install(TARGETS ${target}
    RUNTIME DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
  )
endfunction()

add_executable(g1_ros1_sdk src/g1_ros1_sdk.cpp)
configure_g1_target(g1_ros1_sdk)

if(BUILD_CONTROL_TOOLS)
  message(WARNING "BUILD_CONTROL_TOOLS=ON: actuator-capable G1 utilities will be built")
  add_executable(g1_control src/g1_control.cpp)
  configure_g1_target(g1_control)
  add_executable(g1_arm src/g1_arm.cpp)
  configure_g1_target(g1_arm)
else()
  message(STATUS "G1 actuator-capable control tools are disabled")
endif()

