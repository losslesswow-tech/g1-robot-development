# syntax=docker/dockerfile:1.7

# The private registry cannot resolve its digest anonymously, so use the local
# tag and independently verify the inspected payload below before overlaying
# any files.  The navigation layer remains separate from the ARM64 G1 bridge.
ARG NAVIGATION_BASE_IMAGE=registry.example.invalid/robot/navigation:example
FROM ${NAVIGATION_BASE_IMAGE}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN printf '%s  %s\n' \
      6ddcf76ac40769a5429933773826867389b91cb33d5949d503a0b4f6d6b974fa /nav/w_nav \
      d91b2510e78418a59f0306cd294157844ef7b077a7a0273bc5cb22fe00b9a3a3 /nav/x_planner \
      5b41757d5bf0700ea3567a6d04b6c4b068a7d6030a0afd5899261577dfef7fe6 /nav/x_slam \
      dcbec9cfe1469fa4cad3ece7565e6b81b1b45d9b32da1723691897dcc63d5bf0 /run.sh \
    | sha256sum --check --strict -

COPY scripts/g1_navigation_entrypoint.sh /usr/local/bin/g1-navigation-entrypoint
COPY scripts/validate_overlay.py /usr/local/bin/validate-g1-navigation-overlay
COPY scripts/compat_run.sh /run.sh
COPY config/x_nav.yaml /nav_map/x_nav.yaml
COPY config/runtime_config.py /catkin_ws/src/x_nav_control/scripts/config.py

RUN chmod 0555 /usr/local/bin/g1-navigation-entrypoint \
      /usr/local/bin/validate-g1-navigation-overlay /run.sh \
 && test "$(dpkg --print-architecture)" = "amd64" \
 && test -r /opt/ros/noetic/setup.bash \
 && test -r /catkin_ws/devel/setup.bash \
 && test -x /nav/x_slam \
 && test -x /nav/x_planner \
 && test "${ROS_DISTRO}" = "noetic" \
 && /usr/local/bin/validate-g1-navigation-overlay

ENV ROS_DISTRO=noetic \
    ROS_MASTER_URI=http://192.0.2.6:11311 \
    ROS_IP=192.0.2.6 \
    DDS_INTERFACE=eth0 \
    ROS_DOMAIN_ID=0 \
    ENABLE_MOTION=false \
    AUTO_START=false \
    NAVIGATION_AUTO_START=false \
    ALLOW_CMD_VEL=false \
    REQUIRE_LOCAL_ROS_IP=true \
    X_NAV_CONFIG=/nav_map/x_nav.yaml

ENTRYPOINT ["/usr/local/bin/g1-navigation-entrypoint"]
CMD ["hold"]
