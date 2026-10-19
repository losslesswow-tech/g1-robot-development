#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")"
lidar_trial=0
if [[ "${1:-}" == --lidar-unicast-trial ]]; then lidar_trial=1; shift; fi
[[ $# == 0 ]] || { printf '%s\n' 'Unsupported check argument'; exit 1; }
for tool in python3 ip ssh scp sudo docker iptables; do command -v "$tool" >/dev/null; done
python3 network_probe.py host --preserve-source
printf '%s\n' 'This is a temporary network check, not navigation.' \
  'Cancel navigation and terminate tasks. Robot must be stationary, supervised, with hardware remote stop available.' \
  'Do not start navigation or tasks while this check runs.' \
  'Restoring connectivity can reconnect existing robot processes. No motion commands will be sent by this check.' \
  'The existing G1 motion bridge will be temporarily paused before host routing changes and resumed only after rollback.' \
  'Six temporary NAT exemptions preserve .123.10 only for UDP/ICMP to .123.120/.161/.164; existing MASQUERADE rules stay in place.' \
  'Type STATIONARY to run the check (anything else exits without changes):'
if [[ "$lidar_trial" == 1 ]]; then
  printf '%s\n' 'This run additionally restarts sensor_node only using an isolated Livox Search unicast candidate.' \
    'The original installed executable is restored immediately after startup; the original sensor process is restored at cleanup or within 75 seconds.' \
    'Lidar initialization uses the existing configuration. No navigation Web restart, motion bridge restart, mode change or motion API.'
  docker exec nav_robot python3 /opt/g1-arrival-voice/wifi-check-20261010/livox_trial.py plan
fi
read -r field_ready </dev/tty
[[ "$field_ready" == STATIONARY ]] || exit 1
python3 network_probe.py host --check-state --preserve-source
token=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
socket="$PWD/ssh-$token"
remote_file="/tmp/g1-wifi-probe-$token.py"
remote_discovery="/tmp/g1-dds-discovery-$token.py"
remote_peers="/tmp/g1-dds-peers-$token.json"
remote_started=0
host_started=0
sensor_trial_started=0
cleanup() {
  local original_code=$?
  trap - EXIT INT TERM
  set +e
  if [[ "$sensor_trial_started" == 1 ]]; then
    printf '%s\n' 'Restoring original managed Livox sensor before network rollback:'
    docker exec nav_robot timeout 25 python3 /opt/g1-arrival-voice/wifi-check-20261010/livox_trial.py rollback --token "$token"
    if [[ $? != 0 ]]; then
      printf '%s\n' 'Sensor rollback not confirmed; keep navigation stopped and report this output.'
      original_code=1
    fi
  fi
  if [[ "$host_started" == 1 ]]; then
    if sudo test -f "/run/g1-wifi-probe-$token/state.json"; then
      touch "/tmp/g1-wifi-stop-$token"
      printf '%s\n' 'Navigation-host rollback:'
      sudo python3 network_probe.py rollback --token "$token"
      for n in 1 2 3 4 5; do
        if [[ -f "/tmp/g1-wifi-packets-$token.json" ]]; then
          cat "/tmp/g1-wifi-packets-$token.json"
          printf '\n'
          break
        fi
        sleep 1
      done
      [[ -f "/tmp/g1-wifi-packets-$token.json" ]] || printf '%s\n' 'Host passive report unavailable; check rollback separately.'
    fi
  fi
  if [[ "$remote_started" == 1 ]]; then
    printf '%s\n' 'Robot rollback:'
    ssh -S "$socket" -o BatchMode=yes operator@192.0.2.18 "touch /tmp/g1-wifi-stop-$token"
    ssh -S "$socket" -o BatchMode=yes operator@192.0.2.18 \
      "for n in 1 2 3 4 5; do if test -f /tmp/g1-wifi-result-$token.json; then cat /tmp/g1-wifi-result-$token.json; exit 0; fi; sleep 1; done; exit 1"
    if [[ $? != 0 ]]; then
      printf '%s\n' 'Robot rollback report unavailable; do not start navigation. If setup was applied, its watchdog removes temporary resources within 180 seconds.'
    fi
    ssh -S "$socket" -o BatchMode=yes operator@192.0.2.18 \
      "for n in 1 2 3 4 5; do if test -f /tmp/g1-wifi-packets-$token.json; then cat /tmp/g1-wifi-packets-$token.json; printf '\\n'; exit 0; fi; sleep 1; done; exit 1" \
      || printf '%s\n' 'Passive header report unavailable; the network rollback receipt is reported separately.'
  fi
  ssh -S "$socket" -o BatchMode=yes -O exit operator@192.0.2.18 >/dev/null 2>&1
  printf '%s\n' "Probe finished; temporary configuration is not installed for navigation. Run token: $token"
  exit "$original_code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
printf '%s\n' 'Connecting to the robot; enter SSH password in this terminal.'
ssh -M -S "$socket" -o ControlMaster=yes -o ControlPersist=180 \
  -o ServerAliveInterval=10 -o ServerAliveCountMax=2 -fNT operator@192.0.2.18
scp -o ControlPath="$socket" network_probe.py "operator@192.0.2.18:$remote_file"
scp -o ControlPath="$socket" dds_discovery.py "operator@192.0.2.18:$remote_discovery"
printf '%s\n' 'The next robot sudo prompt is for 35 seconds of DDS reception on eth0.' \
  'A socket temporarily joins 239.255.0.1; the kernel may send IGMP membership reports.' \
  'No DDS participant, motion API or application packet send is used; membership closes before routing changes.'
discovery_code=0
ssh -tt -S "$socket" operator@192.0.2.18 \
  "umask 077; sudo /usr/bin/python3 $remote_discovery --join-spdp --seconds 35 > $remote_peers" || discovery_code=$?
# Empty discovery deliberately returns 1. Retrieve its report before set -e exits.
if scp -o ControlPath="$socket" "operator@192.0.2.18:$remote_peers" "dds-$token.json"; then
  cat "dds-$token.json"
  printf '\n'
else
  printf '%s\n' "DDS discovery report unavailable: $remote_peers; network configuration has not been applied."
  exit 1
fi
if [[ "$discovery_code" != 0 ]]; then
  printf '%s\n' "DDS discovery exited with code $discovery_code; network configuration has not been applied. Keep navigation stopped."
  exit "$discovery_code"
fi
python3 -c 'import json,re,sys; p=json.load(open(sys.argv[1])); peers=p["peers"]; assert p["packet_sends"]==0 and isinstance(peers,list) and 0<len(peers)<=64; assert all(isinstance(v,str) and re.fullmatch(r"192[.]168[.]123[.](161|164):[0-9]{1,5}",v) and 0<int(v.split(":")[1])<=65535 for v in peers); print("\n".join(peers))' "dds-$token.json" > "peers-$token.txt"
mapfile -t dds_peers < "peers-$token.txt"
ssh -S "$socket" operator@192.0.2.18 "chmod 600 $remote_file; python3 $remote_file robot"
printf '%s\n' 'The following sudo prompt is on the robot.' \
  'Temporary return routing, one proxy ARP entry and UDP/ICMP forwarding exceptions for .123.10 and .123.120/.161 are added.' \
  'The default firewall policy stays unchanged; uniquely tagged rules are removed by rollback.'
remote_started=1
ssh -tt -S "$socket" operator@192.0.2.18 \
  "sudo /usr/bin/python3 $remote_file robot --apply --stationary-confirmed --passive-observer --token $token --seconds 180"
printf '%s\n' 'The following sudo prompt is on the navigation host.' \
  'Six run-tagged NAT exemptions are added before existing MASQUERADE rules; they are removed on rollback.'
host_started=1
sudo python3 network_probe.py host --apply --stationary-confirmed --preserve-source --passive-observer --token "$token" --seconds 180
for destination in 192.0.2.8 192.0.2.10 192.0.2.14; do
  ping -I 192.0.2.6 -c 2 -W 1 "$destination" || true
done
printf '%s\n' 'Reading FSM and IMU over Wi-Fi (no motion API).'
probe_ok=0
if docker exec nav_robot timeout 25 /opt/g1-arrival-voice/wifi-check-20261010/build/g1_wifi_readonly --peers "${dds_peers[@]}"; then
  probe_ok=1
else
  printf '%s\n' 'DDS probe did not pass. Waiting briefly for old ARP entries to expire, then retrying once.'
  sleep 15
  if docker exec nav_robot timeout 25 /opt/g1-arrival-voice/wifi-check-20261010/build/g1_wifi_readonly --peers "${dds_peers[@]}"; then probe_ok=1; fi
fi
if [[ "$lidar_trial" == 1 ]]; then
  printf '%s\n' 'Starting the bounded sensor-only unicast discovery trial.'
  sensor_trial_started=1
  docker exec nav_robot timeout 25 python3 /opt/g1-arrival-voice/wifi-check-20261010/livox_trial.py apply --stationary-confirmed --token "$token"
  sleep 3
fi
printf '%s\n' 'Observing existing sensor ROS topics for ten seconds (no navigation request).'
sensor_code=0
docker exec nav_robot bash -c 'source /catkin_ws/devel/setup.bash && export ROS_MASTER_URI=http://192.0.2.5:11311 ROS_IP=192.0.2.5 && timeout 18 python3 /opt/g1-arrival-voice/wifi-check-20261010/sensor_probe.py' || sensor_code=$?
if [[ "$sensor_code" != 0 ]]; then
  printf '%s\n' 'No fresh ROS sensors: inspecting local publisher TCPROS handshakes and message counts before rollback.'
  docker exec nav_robot timeout 20 python3 /opt/g1-arrival-voice/wifi-check-20261010/ros_transport_probe.py || true
  exit "$sensor_code"
fi
[[ "$probe_ok" == 1 ]]
