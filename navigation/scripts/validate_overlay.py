#!/usr/bin/env python3
"""Validate the runtime files that keep the navigation image read-only."""

from pathlib import Path

import yaml


config_path = Path("/nav_map/x_nav.yaml")
runtime_config_path = Path("/catkin_ws/src/x_nav_control/scripts/config.py")
compat_run_path = Path("/run.sh")

config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
assert config.get("tasks") == [], "task commands must be empty"

x_nav = config["x_nav"]
assert x_nav["robot_model"] == "G1_REMOTE_READ_ONLY"
assert x_nav["sensor_lidar"] == "DISABLED"
assert x_nav["planner_run"] is False
assert x_nav["slam_run"] is False
assert x_nav["auto_check_flags"] == []
assert x_nav["auto_run_commands_flags"] == []
assert x_nav["commands_config"] == {}
assert x_nav["planner"]["topic_vel_out"] == "/g1_navigation/blocked_cmd_vel"

yaml_text = config_path.read_text(encoding="utf-8")
assert "rostopic pub" not in yaml_text
compile(runtime_config_path.read_text(encoding="utf-8"), str(runtime_config_path), "exec")
assert "g1-navigation-entrypoint" in compat_run_path.read_text(encoding="utf-8")

print("overlay validation passed")
