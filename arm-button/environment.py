"""G1 backpack environment: validate, stage/build, or read state; no motion entry point.

SSH credentials stay outside the profile and package. Host-key checking is mandatory.
Build outputs are persistent on the host and visible through the existing read-only
development mount. No container/service restart or startup registration is performed.
"""
import argparse
import base64
import hashlib
import io
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parent
DEFAULT_PROFILE = ROOT / 'environments/backpack-20261013.json'


def load_profile(path):
    p = json.loads(Path(path).read_text(encoding='utf-8'))
    if p.get('schema_version') != 1 or p.get('robot_model') != 'Unitree G1':
        raise ValueError('unsupported profile/model')
    if p.get('motion_paused') is not True or p.get('dds_domain') != 0:
        raise ValueError('adapter requires motion_paused=true and DDS domain 0')
    for field in ('management_host', 'ros_ip'):
        ipaddress.IPv4Address(p[field])
    for field in ('ssh_user', 'navigation_container', 'bridge_container', 'dds_interface'):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', p[field]):
            raise ValueError('invalid ' + field)
    for field in ('host_development_root', 'container_development_root'):
        value = PurePosixPath(p[field])
        if not value.is_absolute() or '..' in value.parts or len(value.parts) < 3:
            raise ValueError('invalid ' + field)
    if p['ros_master_uri'] != 'http://' + p['ros_ip'] + ':11311':
        raise ValueError('ROS master/IP mismatch')
    return p


def package():
    files = []
    for name in ('control', 'readonly', 'web', 'poses', 'environments'):
        files.extend(x for x in (ROOT / name).rglob('*') if x.is_file())
    files.extend(ROOT / name for name in ('README.md', 'OFFICIAL_DIMENSIONS.md',
                                         'ENVIRONMENT.md', 'environment.py', 'server.py'))
    entries = {}
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for path in sorted(files):
            if path.is_symlink() or '__pycache__' in path.parts:
                raise ValueError('unexpected source file: ' + str(path))
            name = path.relative_to(ROOT).as_posix()
            data = path.read_bytes()
            entries[name] = hashlib.sha256(data).hexdigest()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    return stream.getvalue(), entries, '20261014-' + digest[:16]


REMOTE = r"""
import base64, hashlib, io, json, os, pathlib, subprocess, tarfile, tempfile, urllib.request
p = PAYLOAD['profile']
container = p['navigation_container']
def run(args, timeout=30):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
def inspect():
    data = json.loads(subprocess.check_output(['docker', 'inspect', container, p['bridge_container']], text=True))
    return [{'name': x['Name'], 'running': x['State']['Running'], 'started_at': x['State']['StartedAt'],
             'network': x['HostConfig']['NetworkMode'],
             'restart': x['HostConfig']['RestartPolicy']['Name']} for x in data]
before = inspect()
report = {'action': PAYLOAD['action'], 'containers_before': before, 'motion_commands_sent': 0,
          'services_restarted': False, 'motion_paused': True}
if any(not x['running'] or x['network'] != 'host' for x in before):
    raise RuntimeError('expected running host-network containers; adapter will not start them')
info = json.loads(subprocess.check_output(['docker','inspect',container], text=True))[0]
env = dict(x.split('=',1) for x in info['Config']['Env'] if '=' in x)
if env.get('ROS_IP') != p['ros_ip'] or env.get('ROS_MASTER_URI') != p['ros_master_uri']:
    raise RuntimeError('active container ROS environment differs from profile')
if not any(m['Source']==p['host_development_root'] and m['Destination']==p['container_development_root']
           and not m['RW'] for m in info['Mounts']):
    raise RuntimeError('persistent read-only development mount missing')
report['interface'] = run(['ip','-4','-brief','address','show','dev',p['dds_interface']])
if report['interface']['returncode'] or p['ros_ip']+'/' not in report['interface']['stdout']:
    raise RuntimeError('profile IP is absent on DDS interface')
host_root = pathlib.Path(p['host_development_root']) / 'arm-button'
container_root = pathlib.PurePosixPath(p['container_development_root']) / 'arm-button'
action = PAYLOAD['action']
if action == 'check':
    report['http'] = {}
    for field in ('preview_url', 'navigation_url', 'hand_readonly_url', 'hand_control_url'):
        try:
            with urllib.request.urlopen(p[field], timeout=5) as response:
                report['http'][field] = response.status
        except Exception as error:
            report['http'][field] = str(error)
else:
    release_id = PAYLOAD.get('release')
    if action != 'deploy' and not release_id:
        current = json.loads((host_root / 'current.json').read_text())
        release_id = current['release']
    import re
    if not re.fullmatch(r'20261014-[0-9a-f]{16}', release_id or ''):
        raise RuntimeError('invalid release identifier')
    release = host_root / 'releases' / release_id
    c_release = str(container_root / 'releases' / release_id)
    report['release'] = release_id
    report['host_release'] = str(release)
    report['container_release'] = c_release
    if action == 'deploy':
        host_root.mkdir(parents=True, exist_ok=True)
        (host_root / 'releases').mkdir(exist_ok=True)
        entries = PAYLOAD['entries']
        if not release.exists():
            staged = pathlib.Path(tempfile.mkdtemp(prefix='stage-', dir=str(host_root)))
            source = staged / 'source'
            source.mkdir()
            with tarfile.open(fileobj=io.BytesIO(base64.b64decode(PAYLOAD['archive'])), mode='r:gz') as archive:
                for member in archive:
                    rel = pathlib.PurePosixPath(member.name)
                    if not member.isfile() or rel.is_absolute() or '..' in rel.parts or member.name not in entries:
                        raise RuntimeError('invalid archive member')
                    data = archive.extractfile(member).read()
                    if hashlib.sha256(data).hexdigest() != entries[member.name]:
                        raise RuntimeError('source digest mismatch')
                    target = source.joinpath(*rel.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            (staged / 'manifest.json').write_text(json.dumps(entries, indent=2))
            staged.rename(release)
        for name, digest in entries.items():
            if hashlib.sha256((release/'source'/name).read_bytes()).hexdigest() != digest:
                raise RuntimeError('persistent source mismatch: '+name)
        build = '/var/tmp/g1-arm-build-' + release_id
        build_script = '''set -eu
source /opt/ros/noetic/setup.bash
cmake -S "$1/source/readonly" -B "$2/readonly" -DCMAKE_BUILD_TYPE=Release
cmake --build "$2/readonly" --parallel 2
cmake -S "$1/source/control" -B "$2/control" -DCMAKE_BUILD_TYPE=Release -DG1_ENABLE_APPROVED_TRIAL=OFF
cmake --build "$2/control" --parallel 2
'''
        build_result = run(['docker','exec',container,'bash','-c',build_script,'_',c_release,build], timeout=110)
        (release/'build.log').write_text(build_result['stdout']+build_result['stderr'])
        report['build'] = {'returncode': build_result['returncode'], 'log': str(release/'build.log')}
        if build_result['returncode']:
            print(json.dumps(report)); raise SystemExit(1)
        (release/'bin').mkdir(exist_ok=True)
        for subdir, binary in [('readonly','g1_arm_state'), ('readonly','g1_mode_query'), ('control','g1_right_arm_bounded'), ('control','g1_arm_service')]:
            # Stream only this file: docker cp can remount unrelated live executable
            # bind mounts and fail with ETXTBSY on this migrated Docker runtime.
            data = subprocess.check_output(['docker','exec',container,'cat',build+'/'+subdir+'/'+binary], timeout=20)
            destination = release/'bin'/binary
            destination.write_bytes(data)
            destination.chmod(0o755)
        report['physical_trial_enabled'] = False
        report['source_files_verified'] = len(entries)
    command = None
    if action in ('deploy', 'self-test'):
        command = [c_release+'/bin/g1_right_arm_bounded', '--self-test']
    elif action == 'observe':
        command = [c_release+'/bin/g1_arm_state', p['dds_interface'], str(PAYLOAD['seconds'])]
    elif action == 'query-mode':
        command = [c_release+'/bin/g1_mode_query', p['dds_interface']]
    elif action == 'service-query':
        command = [c_release+'/bin/g1_arm_service', '--query', p['dds_interface']]
    if command:
        result = run(['docker','exec','-e','LD_LIBRARY_PATH=/opt/ros/noetic/lib:/usr/local/lib',container]+command, timeout=50)
        report['result'] = result
        if result['returncode']:
            print(json.dumps(report)); raise SystemExit(1)
    if action == 'deploy':
        report['binaries_sha256'] = {x.name: hashlib.sha256(x.read_bytes()).hexdigest() for x in (release/'bin').iterdir()}
        temp = host_root / 'current.json.tmp'
        temp.write_text(json.dumps({'release': release_id, 'motion_paused': True, 'physical_trial_enabled': False}))
        temp.replace(host_root/'current.json')
report['containers_after'] = inspect()
if report['containers_after'] != before:
    report['unexpected_service_change'] = True
    print(json.dumps(report)); raise SystemExit(1)
print(json.dumps(report))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('validate', 'check', 'deploy', 'self-test', 'observe', 'query-mode', 'service-query'))
    parser.add_argument('--profile', type=Path, default=DEFAULT_PROFILE)
    parser.add_argument('--ssh-key', type=Path)
    parser.add_argument('--ssh', default='ssh')
    parser.add_argument('--seconds', type=int, choices=range(1, 11), default=3)
    parser.add_argument('--release')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    p = load_profile(args.profile)
    if args.action == 'validate':
        print(json.dumps({'profile_valid': True, 'motion_paused': True, 'host': p['management_host']}))
        return 0
    if not args.ssh_key or not args.ssh_key.is_file():
        parser.error('--ssh-key must point to an existing private key (contents are never copied)')
    payload = {'action': args.action, 'profile': p, 'seconds': args.seconds, 'release': args.release}
    if args.action == 'deploy':
        archive, entries, release = package()
        payload.update(archive=base64.b64encode(archive).decode(), entries=entries, release=release)
    source = 'PAYLOAD = json.loads(base64.b64decode(' + repr(base64.b64encode(json.dumps(payload).encode()).decode()) + '))\n'
    source = 'import base64, json\n' + source + REMOTE
    result = subprocess.run([args.ssh, '-i', str(args.ssh_key), '-o', 'IdentitiesOnly=yes',
                             '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                             '-o', 'ConnectTimeout=8', p['ssh_user']+'@'+p['management_host'],
                             'python3 -'], input=source, text=True, encoding='utf-8',
                            capture_output=True, timeout=150)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({'ssh_returncode': result.returncode, 'stdout': result.stdout,
                                          'stderr': result.stderr}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(result.stdout, end='')
    if result.stderr:
        print(result.stderr, end='')
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
