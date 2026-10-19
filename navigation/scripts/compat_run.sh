#!/usr/bin/env bash
set -euo pipefail

# The vendor base image used /run.sh to launch the web controller, which could
# start robot-specific SDK nodes and a planner publishing /cmd_vel.  Preserve
# the path for compatibility, but route it through the safe entrypoint.
if (( $# == 0 )); then
  set -- hold
fi
exec /usr/local/bin/g1-navigation-entrypoint "$@"
