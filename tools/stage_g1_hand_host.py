"""Build and stage an independent host installer; never execute it remotely."""
import base64
import hashlib
import io
import json
from pathlib import Path
import shlex
import urllib.request
import zipfile


FILES = ('tools/inspire_hand_ctl.py', 'tools/inspire_hand_profile.example.json',
         'tools/install_inspire_hand_host.sh', 'tests/test_inspire_hand_ctl.py',
         'docs/INSPIRE_HAND_DEVELOPMENT.md', 'docs/INSPIRE_HAND_READONLY_20261012.md')


def remote(source):
    request = urllib.request.Request(
        'http://192.0.2.5:9000/api/system/execute_cmd',
        data=json.dumps({'command': 'python3 -c ' + shlex.quote(source)}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        envelope = json.load(response)
    return json.loads(envelope['output'])


def package(root):
    source = root / '_repo_inspection' / 'g1-robot'
    members = {name: (source / name).read_bytes().replace(b'\r\n', b'\n') for name in FILES}
    manifest = {'source_revision': '62d130c63f991d6f70123cc31e5640331eb55d83',
                'upstream_main_checked': 'dd6ea93c632247651464308abae847f234b2ccf4',
                'live_telemetry_tested': True, 'live_motion_tested': False,
                'sha256': {name: hashlib.sha256(data).hexdigest() for name, data in members.items()}}
    readme = ('Independent G1 Inspire RH56DFTP CLI, Python 3.8+.\n'
              'All write commands default to OFFLINE preview. Read docs/INSPIRE_HAND_DEVELOPMENT.md.\n'
              'Host installer runs offline tests and FC03 status only. No service or navigation change.\n'
              'Firmware and per-channel motion calibration remain unverified.\n')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        archive.writestr('MANIFEST.json', json.dumps(manifest, indent=2))
        archive.writestr('README.txt', readme)
    return buffer.getvalue()


def main():
    root = Path(__file__).resolve().parents[1]
    raw = package(root)
    sha = hashlib.sha256(raw).hexdigest()
    target = '/tmp/g1-inspire-hand-' + sha[:12] + '.zip'
    entry = '/tmp/g1-hand-install-20261012.sh'
    bootstrap = ('#!/usr/bin/env bash\nset -euo pipefail\numask 077\n'
                 '[[ $(id -un) == operator && ${HOME:-} == /home/operator ]] || '
                 '{ printf "%s\\n" "Run as operator on the navigation host." >&2; exit 1; }\n'
                 '[[ ! -f /.dockerenv ]] || { printf "%s\\n" "Run on the host, not inside a container." >&2; exit 1; }\n'
                 'g1_hand_stage=$(mktemp -d /home/operator/g1-hand-install.XXXXXXXX)\n'
                 'docker cp nav_robot:' + target + ' "$g1_hand_stage/module.zip"\n'
                 'printf "%s  %s\\n" ' + shlex.quote(sha) + ' "$g1_hand_stage/module.zip" | sha256sum -c -\n'
                 'python3 - "$g1_hand_stage/module.zip" "$g1_hand_stage/install.sh" <<\'PY\'\n'
                 'import pathlib,sys,zipfile\n'
                 'with zipfile.ZipFile(sys.argv[1]) as z:\n'
                 ' pathlib.Path(sys.argv[2]).write_bytes(z.read("tools/install_inspire_hand_host.sh"))\n'
                 'PY\n'
                 'bash "$g1_hand_stage/install.sh" ' + shlex.quote(target) + ' ' + shlex.quote(sha) + '\n')
    artifacts = root / 'artifacts'
    artifacts.mkdir(exist_ok=True)
    package_path = artifacts / ('g1-inspire-hand-host-' + sha[:12] + '.zip')
    package_path.write_bytes(raw)
    (artifacts / 'g1-hand-install-20261012.sh').write_text(bootstrap, encoding='utf-8', newline='\n')
    # Exclusive creation or exact-content reuse: never replace an existing remote file.
    encoded = base64.b64encode(raw).decode()
    script = ('import base64,hashlib,json,pathlib\n'
              'p=pathlib.Path({!r});data=base64.b64decode({!r})\n'
              'if p.exists() or p.is_symlink():\n'
              ' assert not p.is_symlink() and p.read_bytes()==data,"Existing package differs"\n'
              'else:\n'
              ' with p.open("xb") as f:f.write(data)\n'
              'assert hashlib.sha256(p.read_bytes()).hexdigest()=={!r}\n'
              'print(json.dumps({{"staged_package":str(p),"sha256":{!r}}}))\n').format(target, encoded, sha, sha)
    receipt = remote(script)
    encoded = base64.b64encode(bootstrap.encode()).decode()
    script = ('import base64,json,pathlib,subprocess,zipfile\n'
              'data=base64.b64decode({!r});p=pathlib.Path({!r})\n'
              'checks=[subprocess.run(["bash","-n"],input=data,capture_output=True)]\n'
              'with zipfile.ZipFile({!r}) as z:\n'
              ' checks.append(subprocess.run(["bash","-n"],input=z.read("tools/install_inspire_hand_host.sh"),capture_output=True))\n'
              'for r in checks:assert r.returncode==0,r.stderr.decode()\n'
              'if p.exists() or p.is_symlink():\n'
              ' assert not p.is_symlink() and p.read_bytes()==data,"Existing installer differs"\n'
              'else:\n'
              ' with p.open("xb") as f:f.write(data)\n'
              'print(json.dumps({{"installer":str(p),"bash_syntax_passed":True,"host_install_performed":False,"live_writes":0}}))\n').format(encoded, entry, target)
    receipt.update(remote(script))
    receipt['local_package'] = str(package_path)
    receipt['commands'] = ['docker cp nav_robot:' + entry + ' ' + entry, 'bash ' + entry]
    (artifacts / 'g1-hand-host-staged-20261012.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
