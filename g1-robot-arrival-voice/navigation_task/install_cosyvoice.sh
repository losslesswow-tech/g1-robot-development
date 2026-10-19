#!/usr/bin/env bash
# Update the host audio service only. Never reload the navigation Web/API or robot.
set -euo pipefail
umask 077
test "$(id -un)" = operator || { echo 'Run as operator on the navigation host.'; exit 1; }
test "$(hostname)" = operator-2204 || { echo 'Wrong navigation host.'; exit 1; }
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
base=/home/operator/.local/share/g1-task-voice
dropin=/etc/systemd/system/g1-task-voice.service.d/30-cosyvoice.conf
test -d "$base/releases"
current=$(systemctl show g1-task-voice.service --property=WorkingDirectory --value)
case "$current" in "$base"/releases/*) ;; *) echo 'Unexpected existing audio release; refusing update.'; exit 1;; esac
test -d "$current"
test -f "$current/bin/ssh"
test ! -e "$dropin" || { echo 'CosyVoice override already exists; inspect or use its rollback first.'; exit 1; }
# Refuse an active task or in-flight SSH worker; leave all services untouched.
check_idle() {
python3 - <<'PY'
import json, pathlib, subprocess, urllib.request
with urllib.request.urlopen('http://127.0.0.1:9000/api/task/voice_status', timeout=3) as r:
    status=json.load(r)
assert not status.get('managed'), 'Wait for the current task to finish before updating audio'
pid=int(subprocess.check_output(['systemctl','show','g1-task-voice.service','--property=MainPID','--value']))
assert pid > 0, 'Existing audio service must be active'
for p in pathlib.Path('/proc/{}/task'.format(pid)).glob('*/children'):
    assert not p.read_text().strip(), 'Audio SSH worker is active; wait before updating'
with urllib.request.urlopen('http://127.0.0.1:18768/api/state', timeout=3) as r:
    assert json.load(r).get('live'), 'Existing audio service is not ready'
print('Existing task is idle; audio service ready. No navigation or playback requested.')
PY
}
check_idle
config_sha=$(sha256sum /home/operator/g1-arrival-audio-test.ZxVYYQe5/hardware/config.json | cut -d ' ' -f 1)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
release="$base/releases/cosyvoice-$stamp"
backup="$base/backups/cosyvoice-$stamp"
mkdir "$release" "$backup"
# Preserve the exact installed runtime and restricted-key wrapper.
cp -a "$current/." "$release/"
cp arrival_voice/web.py arrival_voice/cosyvoice.py arrival_voice/cosyvoice.example.json "$release/arrival_voice/"
cp navigation_task/install_cosyvoice.sh "$backup/install_cosyvoice.sh"
systemctl cat g1-task-voice.service > "$backup/previous-unit.txt"
python3 - "$release" <<'PY'
import ast, pathlib, sys
root=pathlib.Path(sys.argv[1])
for name in ('arrival_voice/web.py','arrival_voice/cosyvoice.py'):
    ast.parse((root/name).read_text(), filename=name, feature_version=(3,8))
print('New audio code parsed for Python 3.8 compatibility.')
PY
echo 'Enter the CosyVoice API key at the hidden prompt. It is not saved in shell history.'
IFS= read -r -s -p 'CosyVoice API key: ' cosy_key
printf '\n'
test -n "$cosy_key"
case "$cosy_key" in *[[:space:]]*|*\"*|*\'*) echo 'Invalid API key characters.'; exit 1;; esac
cd "$release"
# Validate cloud access without invoking any robot playback code. Failure stops before activation.
DASHSCOPE_API_KEY="" python3 -m arrival_voice.cosyvoice \
  --config arrival_voice/cosyvoice.example.json --text '浙江robot语音合成接口测试。' \
  --output "$backup/cloud-check.wav"
mkdir -p "$base/cosyvoice"
test ! -L "$base/cosyvoice"
config="$base/cosyvoice/config-$stamp.json"
environment="$base/cosyvoice/credentials-$stamp.env"
cp arrival_voice/cosyvoice.example.json "$config"
printf 'DASHSCOPE_API_KEY=%s\n' "$cosy_key" > "$environment"
chmod 600 "$environment" "$config"
unset cosy_key
python3 - "$release" "$config" "$environment" "$backup/new-override.conf" <<'PY'
import pathlib, re, sys
release,config,environment,destination=sys.argv[1:]
assert all(re.fullmatch('/home/operator/[a-zA-Z0-9_./-]+',v) for v in (release,config,environment))
text='''[Service]
WorkingDirectory={release}
Environment=PATH={release}/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile={environment}
ExecStart=
ExecStart=/usr/bin/python3 -m arrival_voice.web --config /home/operator/g1-arrival-audio-test.ZxVYYQe5/hardware/config.json --port 18768 --backend unitree-ssh --ssh-target operator@192.0.2.18 --interface eth0 --allow-robot-audio --tts-provider cosyvoice --tts-config {config}
'''.format(release=release,config=config,environment=environment)
pathlib.Path(destination).write_text(text)
PY
sha=$(sha256sum "$backup/new-override.conf" | cut -d ' ' -f 1)
# Rollback only removes this exact override. The previous unit/runtime remain intact.
cat > "$backup/rollback.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
test "\$(id -un)" = operator
test "\$(hostname)" = operator-2204
path='$dropin'
if sudo test -e "\$path"; then
  actual=\$(sudo sha256sum "\$path" | cut -d ' ' -f 1)
  test "\$actual" = '$sha' || { echo 'Override changed since installation; inspect before rollback.'; exit 1; }
  sudo rm -- "\$path"
fi
sudo systemctl daemon-reload
sudo systemctl restart g1-task-voice.service
echo 'Previous audio runtime restored. No navigation service was restarted.'
EOF
chmod 700 "$backup/rollback.sh"
bash -n "$backup/rollback.sh"
echo 'The following sudo authentication is for the navigation host audio service only.'
sudo -v
sudo install -d -m 755 /etc/systemd/system/g1-task-voice.service.d
# Password entry and cloud generation may take time; check again before activation.
check_idle
test "$(sha256sum /home/operator/g1-arrival-audio-test.ZxVYYQe5/hardware/config.json | cut -d ' ' -f 1)" = "$config_sha"
applied=0
restore_on_error() {
    code=$?
    trap - ERR
    if [ "$applied" = 1 ]; then
        echo 'Audio activation failed; restoring the previous audio service.'
        bash "$backup/rollback.sh" || echo "Automatic audio rollback failed; inspect $backup/rollback.sh"
    fi
    exit "$code"
}
trap restore_on_error ERR
sudo install -m 644 "$backup/new-override.conf" "$dropin"
applied=1
sudo systemctl daemon-reload
sudo systemd-analyze verify g1-task-voice.service
sudo systemctl restart g1-task-voice.service
python3 - <<'PY'
import json,time,urllib.request
for _ in range(30):
    try:
        with urllib.request.urlopen('http://127.0.0.1:18768/api/state',timeout=2) as r:
            state=json.load(r)
        if state.get('live') and state.get('tts',{}).get('provider')=='cosyvoice':
            print(json.dumps({'audio_ready':True,'tts':state['tts'],'points':len(state['config']['points']),
                              'robot_audio_submitted':False}))
            break
    except OSError:
        pass
    time.sleep(1)
else:
    raise SystemExit('New audio service not ready')
PY
trap - ERR
test "$(sha256sum /home/operator/g1-arrival-audio-test.ZxVYYQe5/hardware/config.json | cut -d ' ' -f 1)" = "$config_sha"
printf 'CosyVoice installed. Rollback: bash %s/rollback.sh\n' "$backup"
echo 'No task data, map, navigation threshold, robot worker or SSH key was changed. No robot playback submitted.'
