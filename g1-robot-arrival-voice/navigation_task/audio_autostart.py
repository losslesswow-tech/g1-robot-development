"""Build a restricted audio-only SSH worker and navigation-host systemd unit."""
import argparse
import base64
import hashlib
import json
import re
import shlex
from pathlib import Path

from arrival_voice.ssh_audio import REMOTE


VALIDATE = '''
    cfg = json.loads(sys.stdin.buffer.read(15 * 1024 * 1024 + 1))
    if not isinstance(cfg, dict) or cfg.get("interface") != "eth0" or cfg.get("operation") not in ("probe", "play"):
        raise ValueError("Only eth0 audio probe/play is allowed")
    if cfg["operation"] == "play":
        message = cfg.get("message")
        if not isinstance(message, dict) or not isinstance(cfg.get("task_id"), str) or len(cfg["task_id"]) > 150:
            raise ValueError("Invalid audio request")
        if message.get("mode") == "tts":
            if not isinstance(message.get("text"), str) or not message["text"].strip() or len(message["text"]) > 1000 or type(message.get("speaker_id")) is not int or message["speaker_id"] not in (0, 1):
                raise ValueError("Invalid TTS")
        elif message.get("mode") == "wav":
            import io, wave
            media_data = base64.b64decode(cfg.get("pcm_wav", ""), validate=True)
            if not 0 < len(media_data) <= 10 * 1024 * 1024:
                raise ValueError("Invalid WAV size")
            with wave.open(io.BytesIO(media_data), "rb") as wav:
                if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) != (16000, 1, 2, "NONE") or not 0 < wav.getnframes() <= 16000 * 300 or len(wav.readframes(wav.getnframes())) != wav.getnframes() * 2:
                    raise ValueError("Invalid WAV format")
        else:
            raise ValueError("Only TTS/WAV is allowed")
'''


def worker_source():
    audio = Path(__file__).parents[1] / 'arrival_voice/audio.py'
    assert REMOTE.count('    cfg = json.load(sys.stdin)') == 1
    return audio.read_text(encoding='utf-8') + '\n' + REMOTE.replace('    cfg = json.load(sys.stdin)', VALIDATE.rstrip())


REMOTE_INSTALL = '''import base64,hashlib,json,os,pathlib,shlex,sys,time
p=json.load(sys.stdin)
key=p['public_key'].strip()
parts=key.split()
assert len(parts)==3 and parts[0]=='ssh-ed25519' and parts[2]=='g1-task-voice-autostart'
base64.b64decode(parts[1],validate=True)
worker=base64.b64decode(p['worker'],validate=True)
assert hashlib.sha256(worker).hexdigest()==p['sha256']
compile(worker,'audio-worker','exec')
home=pathlib.Path.home()
assert str(home)=='/home/operator'
root=home/'.local/share/g1-task-voice'
root.mkdir(parents=True,exist_ok=True)
path=root/('worker-'+p['sha256']+'.py')
path.write_bytes(worker);path.chmod(0o600)
command='/usr/bin/env LD_LIBRARY_PATH=/home/operator/cyclonedds_ws/install/cyclonedds/lib /home/operator/g1_voice/.venv/bin/python '+shlex.quote(str(path))
entry='restrict,command="'+command+'" '+key
ssh=home/'.ssh';ssh.mkdir(mode=0o700,exist_ok=True)
auth=ssh/'authorized_keys'
assert not auth.is_symlink()
old=auth.read_text() if auth.exists() else ''
lines=old.splitlines()
existing=[line for line in lines if parts[1] in line.split()]
if existing:
 assert existing==[entry], 'Existing key has unexpected options; refusing to widen access'
else:
 if auth.exists():
  backup=root/('authorized_keys-'+str(time.time_ns())+'.backup')
  backup.write_text(old);backup.chmod(0o600)
 temporary=ssh/'authorized_keys.g1-voice-new'
 with temporary.open('x') as f:f.write(old+('' if not old or old.endswith('\\n') else '\\n')+entry+'\\n')
 temporary.chmod(0o600);os.replace(str(temporary),str(auth))
print(json.dumps({'restricted_audio_key_installed':True,'worker_sha256':p['sha256'],'password_saved':False}))
'''


def unit(release, config):
    for value in (release, config):
        if not re.fullmatch(r'/home/operator/[a-zA-Z0-9_./-]+', value):
            raise ValueError('Unsupported installation path')
    return '''[Unit]
Description=G1 task voice audio backend (no navigation startup)
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=operator
Group=operator
Environment=HOME=/home/operator
Environment=PYTHONUNBUFFERED=1
Environment=PATH={release}/bin:/usr/local/bin:/usr/bin:/bin
WorkingDirectory={release}
ExecStart=/usr/bin/python3 -m arrival_voice.web --config {config} --port 18768 --backend unitree-ssh --ssh-target operator@192.0.2.18 --interface eth0 --allow-robot-audio
Restart=always
RestartSec=10
TimeoutStopSec=15
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
'''.format(release=release, config=config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('payload', 'remote-command', 'unit'))
    parser.add_argument('paths', nargs='*')
    args = parser.parse_args()
    if args.operation == 'payload':
        worker = worker_source().encode()
        key = Path(args.paths[0]).read_text().strip()
        print(json.dumps({'public_key': key, 'worker': base64.b64encode(worker).decode(),
                          'sha256': hashlib.sha256(worker).hexdigest()}))
    elif args.operation == 'remote-command':
        code = 'import base64;exec(base64.b64decode({!r}))'.format(base64.b64encode(REMOTE_INSTALL.encode()).decode())
        print('python3 -c ' + shlex.quote(code))
    else:
        print(unit(*args.paths), end='')


if __name__ == '__main__':
    main()
