"""Bounded sensor-only trial. Default is a read-only plan.

Uses the navigation process manager's sensor_node endpoint. Never controls any
other node. The installed executable is restored immediately after startup;
a separate watchdog then restores the original sensor process within 75 s.
"""
import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

DRIVER = Path('/catkin_ws/devel/lib/livox_ros_driver2/livox_ros_driver2_node')
BASE = Path(__file__).resolve().parent
LOG = Path('/opt/g1-arrival-voice/state/task-web-20261010.log')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def api(endpoint, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request('http://127.0.0.1:9000/api/' + endpoint,
                                    data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=5) as response:
        result = json.load(response)
    if body is not None and not result.get('success'):
        raise RuntimeError('Sensor process manager rejected ' + endpoint)
    return result


def sensor_processes():
    result = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            executable = proc / 'exe'
            name = os.readlink(str(executable)).split(' (deleted)')[0]
            if Path(name).name != DRIVER.name:
                continue
            fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
            result.append({'pid': int(proc.name), 'start_ticks': fields[19],
                           'sha256': digest(executable)})
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return result


def signal_candidate(entry, expected_hash, value):
    # Python 3.8 has no pidfd helpers. Ubuntu x86_64 exposes these Linux syscalls.
    # Bind the descriptor before checking identity; never signal by bare PID.
    if platform.machine() != 'x86_64':
        raise ValueError('Sensor trial signalling is pinned to the verified x86_64 host')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    fd = libc.syscall(434, int(entry['pid']), 0)  # pidfd_open
    if fd < 0:
        if ctypes.get_errno() == errno.ESRCH:
            return
        raise OSError(ctypes.get_errno(), 'pidfd_open')
    try:
        current = next((p for p in sensor_processes() if p['pid'] == entry['pid']), None)
        if not current:
            return
        if current['start_ticks'] != entry['start_ticks'] or current['sha256'] != expected_hash:
            raise ValueError('Sensor identity changed; refusing signal')
        if libc.syscall(424, int(fd), int(value), ctypes.c_void_p(), 0) < 0:  # pidfd_send_signal
            if ctypes.get_errno() != errno.ESRCH:
                raise OSError(ctypes.get_errno(), 'pidfd_send_signal')
    finally:
        os.close(fd)


def validate_manifest(manifest, driver=DRIVER, base=BASE):
    candidate = base / 'build/livox_ros_driver2_unicast'
    if driver.is_symlink() or not driver.is_file() or digest(driver) != manifest['original_sha256']:
        raise ValueError('Installed driver changed or is a symlink; refusing replacement')
    if digest(candidate) != manifest['candidate_sha256']:
        raise ValueError('Candidate hash mismatch')
    return candidate


def config_check(config):
    if [item.get('ip') for item in config.get('lidar_configs', [])] != ['192.0.2.14']:
        raise ValueError('Trial is restricted to the configured .123.120 lidar')
    hosts = config.get('MID360', {}).get('host_net_info', [])
    if len(hosts) != 1 or hosts[0].get('host_ip') != '192.0.2.6':
        raise ValueError('Unexpected Livox host address')


def save(directory, state):
    temporary = directory / 'state.new'
    temporary.write_text(json.dumps(state))
    os.replace(str(temporary), str(directory / 'state.json'))


def restore_executable(directory, state):
    backup = Path(state['backup'])
    if digest(DRIVER) == state['original_sha256']:
        if backup.exists() and digest(backup) == state['original_sha256']:
            backup.unlink()
        return
    if digest(DRIVER) != state['candidate_sha256'] or digest(backup) != state['original_sha256']:
        raise ValueError('Driver changed by another operator; refusing overwrite')
    os.replace(str(backup), str(DRIVER))


def driver_events(state):
    if not LOG.exists():
        return []
    with LOG.open('rb') as handle:
        handle.seek(max(state.get('log_offset', 0), LOG.stat().st_size - 262144))
        lines = handle.read(262144).decode(errors='replace').splitlines()
    return [line for line in lines if any(text in line.lower() for text in
            ('g1_wifi_livox', 'lidar', 'pcl data', 'scan pattern'))][-40:]


def rollback(directory):
    state_path = directory / 'state.json'
    if not state_path.exists():
        return {'phase': 'sensor_not_applied'}
    state = json.loads(state_path.read_text())
    if state.get('restored'):
        if digest(DRIVER) != state['original_sha256']:
            raise ValueError('Executable changed after rollback; refusing further changes')
        return {'phase': 'sensor_already_rolled_back', 'motion_calls': 0}
    restore_executable(directory, state)
    candidate_running = [p for p in sensor_processes() if p['sha256'] == state['candidate_sha256']]
    if candidate_running:
        try:
            api('system/stop_node', {'name': 'sensor_node'})
        except Exception:
            pass  # Still stop a candidate orphaned by the existing manager's restart race.
        for entry in sensor_processes():
            if entry['sha256'] == state['candidate_sha256']:
                signal_candidate(entry, state['candidate_sha256'], signal.SIGTERM)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(p['sha256'] == state['candidate_sha256'] for p in sensor_processes()):
            time.sleep(.1)
        for entry in sensor_processes():
            if entry['sha256'] == state['candidate_sha256']:
                signal_candidate(entry, state['candidate_sha256'], signal.SIGKILL)
    api('system/start_node', {'name': 'sensor_node'})
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        running = sensor_processes()
        if len(running) == 1 and running[0]['sha256'] == state['original_sha256']:
            state['restored'] = True
            save(directory, state)
            return {'phase': 'sensor_rolled_back', 'original_sha256': state['original_sha256'],
                    'sensor': running[0], 'driver_events': driver_events(state), 'motion_calls': 0}
        time.sleep(.25)
    raise RuntimeError('Original sensor process could not be confirmed; keep navigation stopped')


def locked(directory, operation):
    with (directory / 'lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return operation()


def apply(directory, manifest):
    from network_probe import ensure_idle
    ensure_idle()
    import psutil
    addresses = psutil.net_if_addrs()
    if any(a.family == 2 for a in addresses.get('enp132s0', [])):
        raise ValueError('Wired IPv4 is present; refusing the Wi-Fi trial')
    if not any(a.address == '192.0.2.6' and a.netmask == '255.255.255.255'
               for a in addresses.get('wlp131s0f0', [])):
        raise ValueError('Temporary Wi-Fi alias is not active')
    config_check(json.loads(Path('/nav_map/MID360s_config.json').read_text()))
    candidate = validate_manifest(manifest)
    processes = sensor_processes()
    if len(processes) != 1 or processes[0]['sha256'] != manifest['original_sha256']:
        raise ValueError('Expected exactly one original managed sensor process')
    backup = DRIVER.parent / ('.g1-livox-original-' + directory.name)
    replacement = DRIVER.parent / ('.g1-livox-candidate-' + directory.name)
    state = dict(manifest, backup=str(backup), replacement=str(replacement),
                 deadline=time.time() + 75, restored=False,
                 log_offset=LOG.stat().st_size if LOG.exists() else 0)
    save(directory, state)
    with (directory / 'watchdog.log').open('ab') as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'watchdog',
                          '--token', directory.name[len('g1-livox-trial-'):]],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)
    os.link(str(DRIVER), str(backup))
    shutil.copy2(str(candidate), str(replacement))
    os.replace(str(replacement), str(DRIVER))
    try:
        api('system/stop_node', {'name': 'sensor_node'})
        # The verified manager auto-checks sensor_node once per second. Let it
        # restart first, rather than racing its Popen bookkeeping with start_node.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            running = sensor_processes()
            if len(running) == 1 and running[0]['sha256'] == manifest['candidate_sha256']:
                restore_executable(directory, state)
                return {'phase': 'sensor_trial_active', 'sensor': running[0],
                        'installed_executable_restored': True, 'auto_rollback_seconds': 75,
                        'motion_calls': 0}
            time.sleep(.25)
        raise RuntimeError('Candidate sensor did not start')
    except BaseException:
        rollback(directory)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'apply', 'rollback', 'watchdog'], default='plan', nargs='?')
    parser.add_argument('--token')
    parser.add_argument('--stationary-confirmed', action='store_true')
    args = parser.parse_args()
    manifest = json.loads((BASE / 'build/livox-trial-manifest.json').read_text())
    if args.action == 'plan':
        validate_manifest(manifest)
        print(json.dumps({'sensor_only_trial': manifest, 'network_applied': False, 'motion_calls': 0}))
        return
    if os.geteuid() != 0 or not args.token or not re.fullmatch('[0-9a-f]{32}', args.token):
        raise ValueError('Root and a 32-character run token are required')
    directory = Path('/run') / ('g1-livox-trial-' + args.token)
    if args.action == 'apply':
        if not args.stationary_confirmed:
            raise ValueError('Stationary supervised field confirmation is required')
        if any(p.name != directory.name and not json.loads((p/'state.json').read_text()).get('restored')
               for p in Path('/run').glob('g1-livox-trial-*') if (p/'state.json').exists()):
            raise ValueError('Another sensor trial is still active')
        directory.mkdir(mode=0o700, exist_ok=False)
        result = locked(directory, lambda: apply(directory, manifest))
    elif args.action == 'rollback':
        result = locked(directory, lambda: rollback(directory)) if directory.exists() else {'phase': 'sensor_not_applied'}
    else:
        state = json.loads((directory/'state.json').read_text())
        time.sleep(max(0, state['deadline'] - time.time()))
        result = locked(directory, lambda: rollback(directory))
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'phase': 'sensor_trial_error', 'error': str(error), 'motion_calls': 0}), flush=True)
        raise SystemExit(1)
