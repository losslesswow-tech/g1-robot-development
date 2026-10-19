cmake_minimum_required(VERSION 3.10)
project(g1_arm7_teach_play)
set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_BUILD_TYPE Release)

if(NOT DEFINED UNITREE_SDK2_PATH)
    set(UNITREE_SDK2_PATH "/opt/unitree_robotics/unitree_sdk2")
endif()

include_directories(
    ${UNITREE_SDK2_PATH}/include
    ${UNITREE_SDK2_PATH}/include/ddscxx
    ${UNITREE_SDK2_PATH}/thirdparty/include
    ${UNITREE_SDK2_PATH}/thirdparty/include/ddscxx
)

link_directories(
    ${UNITREE_SDK2_PATH}/lib
    ${UNITREE_SDK2_PATH}/thirdparty/lib/aarch64
)

set(UNITREE_LIBS unitree_sdk2 ddscxx ddsc pthread)

add_executable(teach teach.cpp)
target_link_libraries(teach ${UNITREE_LIBS})

add_executable(play play.cpp)
target_link_libraries(play ${UNITREE_LIBS})

add_executable(teach_with_name teach_with_name.cpp)
target_link_libraries(teach_with_name ${UNITREE_LIBS})

add_executable(play_with_motions play_with_motions.cpp)
target_link_libraries(play_with_motions ${UNITREE_LIBS})

add_executable(watchdog watchdog.cpp)
target_link_libraries(watchdog ${UNITREE_LIBS})

add_executable(tts_probe tts_probe.cpp)
target_link_libraries(tts_probe ${UNITREE_LIBS})
