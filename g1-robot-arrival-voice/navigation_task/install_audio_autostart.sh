#!/usr/bin/env bash
# One-time installation on navigation host. No navigation or playback requests.
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
test "$(id -un)" = operator || { echo 'Run as operator on the navigation host.'; exit 1; }
test "$(hostname)" = operator-2204 || { echo 'Wrong navigation host.'; exit 1; }
config=/home/operator/g1-arrival-audio-test.ZxVYYQe5/hardware/config.json
test -f "$config"
python3 - <<'PY'
import socket
with socket.socket() as s:
    if s.connect_ex(('127.0.0.1',18768)) == 0:
        raise SystemExit('Port 18768 is occupied. Stop the manually started voice terminal with Ctrl+C, then rerun this installer. Navigation must not be stopped or restarted by this installer.')
PY
base=/home/operator/.local/share/g1-task-voice
mkdir -p "$base/identity" "$base/releases" "$base/backups"
chmod 700 "$base" "$base/identity"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
release="$base/releases/$stamp"
mkdir "$release"
cp -a arrival_voice navigation_task "$release/"
key="$base/identity/id_ed25519"
if [ ! -f "$key" ]; then
    ssh-keygen -q -t ed25519 -N '' -C g1-task-voice-autostart -f "$key"
fi
test -f "$key.pub"
chmod 600 "$key"
state=$(mktemp -d /tmp/g1-voice-install.XXXXXXXX)
cleanup() {
    /usr/bin/ssh -S "$state/control" -O exit operator@192.0.2.18 >/dev/null 2>&1 || true
    rm -f -- "$state/remote.json"
    rmdir -- "$state" 2>/dev/null || true
}
trap cleanup EXIT
echo 'One-time robot authentication. Enter the SSH password only in this terminal.'
/usr/bin/ssh -F /dev/null -M -S "$state/control" -o ControlPersist=120 -o StrictHostKeyChecking=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -fNT operator@192.0.2.18
# Pin the same previously verified host key; never use ssh-keyscan or accept-new.
ssh-keygen -F 192.0.2.18 -f /home/operator/.ssh/known_hosts > "$base/identity/known_hosts"
test -s "$base/identity/known_hosts"
python3 -m navigation_task.audio_autostart payload "$key.pub" > "$state/remote.json"
remote=$(python3 -m navigation_task.audio_autostart remote-command)
/usr/bin/ssh -F /dev/null -S "$state/control" -o ProxyCommand=false -T operator@192.0.2.18 "$remote" < "$state/remote.json"
# A private wrapper selects only this restricted audio key. Existing transport stays unchanged.
mkdir -p "$release/bin"
cat > "$release/bin/ssh" <<'SH'
#!/usr/bin/env bash
exec /usr/bin/ssh -F /dev/null -i /home/operator/.local/share/g1-task-voice/identity/id_ed25519 -o IdentitiesOnly=yes -o ControlMaster=no -o UserKnownHostsFile=/home/operator/.local/share/g1-task-voice/identity/known_hosts "$@"
SH
chmod 700 "$release/bin/ssh"
export PATH="$release/bin:/usr/local/bin:/usr/bin:/bin"
cd "$release"
# Dedicated key is checked through GetVolume before enabling the service.
python3 -m arrival_voice.ssh_audio --target operator@192.0.2.18 --interface eth0
python3 -m navigation_task.audio_autostart unit "$release" "$config" > "$base/backups/$stamp.service"
echo 'The following sudo authentication is on the navigation host.'
sudo -v
unit=/etc/systemd/system/g1-task-voice.service
if sudo test -e "$unit"; then
    sudo cp -a "$unit" "$base/backups/$stamp.previous.service"
fi
sudo install -m 644 "$base/backups/$stamp.service" "$unit"
sudo systemd-analyze verify "$unit"
sudo systemctl daemon-reload
sudo systemctl enable --now g1-task-voice.service
python3 - <<'PY'
import json,time,urllib.request
for attempt in range(45):
    try:
        with urllib.request.urlopen('http://127.0.0.1:18768/api/state',timeout=2) as r:
            state=json.load(r)
        if state.get('live'):
            print(json.dumps({'voice_service_ready':True,'live':True,'startup_playback':False}))
            break
    except OSError:
        pass
    time.sleep(1)
else:
    raise SystemExit('Service installed; not ready yet. Inspect journalctl -u g1-task-voice.service -n 40. Do not submit playback repeatedly.')
PY
sudo systemctl is-enabled g1-task-voice.service
sudo systemctl is-active g1-task-voice.service
echo 'Audio autostart installed. This terminal may now be closed. No navigation or playback was submitted.'
