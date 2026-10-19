#!/usr/bin/env bash
# Run from the extracted package on the navigation HOST, not inside nav_robot.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
robot_target="${1:-operator@192.0.2.18}"
voice_config="${2:-.local-voice/web/config.json}"
state_dir="$(mktemp -d "${TMPDIR:-/tmp}/g1-arrival-session.XXXXXXXX")"
control_path="$state_dir/ssh-control"
cleanup() {
    ssh -S "$control_path" -O exit "$robot_target" >/dev/null 2>&1 || true
    # Remove only our private socket directory, and only when it is empty.
    rmdir -- "$state_dir" 2>/dev/null || true
}
trap cleanup EXIT
printf '%s\n' 'Connecting to G1. Enter the SSH password in this terminal; it is not saved.'
ssh -M -S "$control_path" -o ControlPersist=yes -o StrictHostKeyChecking=yes \
    -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -fNT "$robot_target"
python3 -m arrival_voice.ssh_audio --target "$robot_target" --interface eth0 --control-path "$control_path"
printf '%s\n' 'Read-only audio probe passed. Opening the web editor; no speech is submitted at startup.'
python3 -m arrival_voice.web --config "$voice_config" --port 18768 \
    --backend unitree-ssh --ssh-target "$robot_target" --ssh-control-path "$control_path" \
    --interface eth0 --allow-robot-audio
