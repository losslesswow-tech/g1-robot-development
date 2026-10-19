# syntax=docker/dockerfile:1.7

# This image is intentionally ARM64-only. --platform selects an ARM64 base
# manifest; it does not translate vendor binaries. The readelf checks below
# independently reject non-AArch64 SDK libraries and bridge executables.
ARG ROS_BASE_IMAGE=ros:noetic-ros-base-focal

FROM --platform=linux/arm64 ${ROS_BASE_IMAGE} AS builder
ARG TARGETARCH

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN test "${TARGETARCH}" = "arm64" \
 && test "$(dpkg --print-architecture)" = "arm64"

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      binutils \
      build-essential \
      ca-certificates \
      cmake \
      file \
      libboost-all-dev \
      libeigen3-dev \
      libfmt-dev \
      libyaml-cpp-dev \
 && rm -rf /var/lib/apt/lists/*

COPY scripts/verify_architecture.sh /usr/local/bin/verify-architecture
RUN chmod 0755 /usr/local/bin/verify-architecture

# unitree_sdk2 must be a complete checkout of Unitree's official repository,
# pinned to an immutable commit. Its CMake files select lib/aarch64 and
# thirdparty/lib/aarch64 when this stage runs on linux/arm64.
COPY unitree_sdk2/ /opt/unitree_sdk2-src/
RUN test -f /opt/unitree_sdk2-src/CMakeLists.txt \
 && test -f /opt/unitree_sdk2-src/include/unitree/robot/g1/loco/g1_loco_client.hpp \
 && /usr/local/bin/verify-architecture arm64 \
      /opt/unitree_sdk2-src/lib/aarch64/libunitree_sdk2.a \
      /opt/unitree_sdk2-src/thirdparty/lib/aarch64/libddsc.so \
      /opt/unitree_sdk2-src/thirdparty/lib/aarch64/libddscxx.so

RUN cmake -S /opt/unitree_sdk2-src -B /tmp/unitree-sdk-build \
      -DBUILD_EXAMPLES=OFF \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=/opt/unitree_robotics \
 && cmake --build /tmp/unitree-sdk-build --target install --parallel "$(nproc)"

ENV LD_LIBRARY_PATH=/opt/unitree_robotics/lib

WORKDIR /catkin_ws
COPY pkg/ /catkin_ws/src/g1_ros1_sdk/
RUN source /opt/ros/noetic/setup.bash \
 && catkin_make install \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=/opt/g1_bridge \
      -DBUILD_CONTROL_TOOLS=OFF \
      -Dunitree_sdk2_DIR=/opt/unitree_robotics/lib/cmake/unitree_sdk2

RUN source /opt/ros/noetic/setup.bash \
 && /usr/local/bin/verify-architecture arm64 \
      /opt/unitree_robotics/lib/libunitree_sdk2.a \
      /opt/unitree_robotics/lib/libddsc.so \
      /opt/unitree_robotics/lib/libddscxx.so \
      /opt/g1_bridge/lib/g1_ros1_sdk/g1_ros1_sdk \
 && ldd /opt/g1_bridge/lib/g1_ros1_sdk/g1_ros1_sdk | tee /tmp/g1-bridge-ldd.txt \
 && ! grep -q 'not found' /tmp/g1-bridge-ldd.txt

FROM --platform=linux/arm64 ${ROS_BASE_IMAGE} AS runtime
ARG TARGETARCH

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV LD_LIBRARY_PATH=/opt/unitree_robotics/lib

RUN test "${TARGETARCH}" = "arm64" \
 && test "$(dpkg --print-architecture)" = "arm64" \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      binutils \
      iproute2 \
      libfmt-dev \
      libyaml-cpp0.6 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/verify-architecture /usr/local/bin/verify-architecture
COPY --from=builder /opt/unitree_robotics/ /opt/unitree_robotics/
COPY --from=builder /opt/g1_bridge/ /opt/g1_bridge/
COPY g1_start.sh /g1_start.sh
COPY scripts/g1_bridge_healthcheck.sh /usr/local/bin/g1-bridge-healthcheck
RUN chmod 0755 /g1_start.sh /usr/local/bin/g1-bridge-healthcheck \
 && source /opt/ros/noetic/setup.bash \
 && /usr/local/bin/verify-architecture arm64 \
      /opt/unitree_robotics/lib/libunitree_sdk2.a \
      /opt/unitree_robotics/lib/libddsc.so \
      /opt/unitree_robotics/lib/libddscxx.so \
      /opt/g1_bridge/lib/g1_ros1_sdk/g1_ros1_sdk \
 && ldd /opt/g1_bridge/lib/g1_ros1_sdk/g1_ros1_sdk | tee /tmp/g1-bridge-ldd.txt \
 && ! grep -q 'not found' /tmp/g1-bridge-ldd.txt

ENV ROS_MASTER_URI=http://192.0.2.6:11311 \
    ROS_IP=192.0.2.8 \
    DDS_INTERFACE=eth0 \
    ROS_DOMAIN_ID=0 \
    ENABLE_MOTION=false \
    AUTO_START=false \
    MOTION_LOCKED=true \
    DEPLOYMENT_MODE=external_navigation \
    REQUIRE_ROS_MASTER=true \
    ROS_MASTER_CHECK_TIMEOUT_SEC=3

ENTRYPOINT ["/g1_start.sh"]
