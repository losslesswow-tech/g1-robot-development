"""Bounded navigation-Web reload; never publish ROS commands or restart the G1 bridge.

The operator must stop tasks and confirm field supervision before `apply`.
Prepare records exact process identities; apply refuses changed processes/files.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path('/opt/g1-arrival-voice/releases/20261009-task-v1')
STATE = Path('/opt/g1-arrival-voice/state')
CONTROLLER = '/catkin_ws/src/x_nav_control/scripts/robot_web_controller.py'


def get(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def process(pid):
    root = Path('/proc') / str(pid)
    fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] == 'Z':
        raise ProcessLookupError(pid)
    return {'pid': int(pid), 'start': fields[19], 'parent': int(fields[1]),
            'args': [v for v in (root / 'cmdline').read_bytes().decode().split('\0') if v]}


def processes():
    result = []
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                result.append(process(entry.name))
            except (OSError, ValueError):
                pass
    return result


def same(record):
    try:
        now = process(record['pid'])
        return now['start'] == record['start'] and now['args'] == record['args']
    except (OSError, ValueError):
        return False


def managed(args):
    return (args[0] in ('/nav/w_nav', '/nav/x_planner', '/nav/x_slam') or
            any(v.endswith('/livox_ros_driver2_node') or v.endswith('/nav_log.py') or
                v.endswith('/system_monitor.py') for v in args) or
            args[:3] == ['python3', '-m', 'http.server'] or
            ('foxglove_bridge' in args and any(v.endswith('/roslaunch') for v in args)))


def descendants(parent, listing):
    found, parents = [], {parent}
    while True:
        new = [p for p in listing if p['parent'] in parents and p['pid'] not in parents]
        if not new:
            return found
        found.extend(new)
        parents.update(p['pid'] for p in new)


def prepare():
    import yaml
    configuration = Path('/nav_map/x_nav.yaml').read_bytes()
    settings = yaml.safe_load(configuration)['x_nav']
    if settings['robot_model'] != 'G1_BRIDGE' or settings['sensor_lidar'] != 'MID_360':
        raise RuntimeError('robot or sensor startup configuration changed')
    enabled = settings['auto_run_commands_flags']
    commands = list(settings['commands_config'].items())
    if len(enabled) != len(commands):
        raise RuntimeError('startup flags need review')
    allowed = {'web_node', 'foxglove_node', 'w_nav_node', 'log_node'}
    if any(flag and name not in allowed for (name, _), flag in zip(commands, enabled)):
        raise RuntimeError('additional automatic startup commands need review')
    listing = processes()
    controllers = [p for p in listing if CONTROLLER in p['args']]
    if len(controllers) != 1:
        raise RuntimeError('expected one original navigation Web process')
    controller = controllers[0]
    parent = next(p for p in listing if p['pid'] == controller['parent'])
    if not any(v.endswith('/roslaunch') for v in parent['args']):
        raise RuntimeError('unexpected Web supervisor')
    # The existing roslaunch keeps its registered core processes; do not signal it.
    core = [p for p in listing if p['parent'] == parent['pid'] and
            any(v.endswith('/rosmaster') or v.endswith('/rosout/rosout') for v in p['args'])]
    if len(core) != 2:
        raise RuntimeError('original ROS core ownership differs; reload aborted')
    roots = [p for p in listing if p['parent'] == controller['pid'] and managed(p['args'])]
    children = roots + [p for root in roots for p in descendants(root['pid'], listing)]
    if any('g1_' in ' '.join(p['args']) and p not in controllers for p in children):
        raise RuntimeError('G1 process in navigation child list; reload aborted')
    listeners = [p for p in listing if 'arrival_voice.legacy_auto' in p['args']]
    if len(listeners) > 1 or any('/opt/g1-arrival-voice/state/legacy.sqlite3' not in p['args'] for p in listeners):
        raise RuntimeError('unexpected arrival listener')
    tasks = json.loads(Path('/nav_map/task.json').read_text())
    if any(t.get('scheduleType') and not t.get('executed', False) for t in tasks):
        raise RuntimeError('scheduled tasks must be disabled before reloading')
    status = get('http://127.0.0.1:9000/api/extra/get_status', 4)
    if status['task_status'] not in ('无任务', ''):
        raise RuntimeError('task is not idle: ' + status['task_status'])
    audio = get('http://127.0.0.1:18768/api/state')
    if not audio.get('live'):
        raise RuntimeError('audio editor is not connected to real hardware')
    manifest = json.loads((ROOT / 'prepared/manifest.json').read_text())
    for name, hashes in manifest['files'].items():
        path = Path(manifest['scripts']) / name
        before = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        if before != hashes['before']:
            raise RuntimeError('navigation source changed: ' + name)
    environment = dict(v.split('=', 1) for v in (Path('/proc') / str(controller['pid']) / 'environ').read_bytes().decode().split('\0') if '=' in v)
    keep = ('PATH', 'PYTHONPATH', 'LD_LIBRARY_PATH', 'CMAKE_PREFIX_PATH', 'ROS_PACKAGE_PATH',
            'ROS_MASTER_URI', 'ROS_IP', 'ROS_HOSTNAME', 'ROS_DISTRO', 'ROS_VERSION', 'ROS_PYTHON_VERSION',
            'ROS_ROOT', 'HOME', 'LANG', 'DISPLAY')
    return {'controller': controller, 'core': core, 'children': children, 'listeners': listeners,
            'environment': {k: environment[k] for k in keep if k in environment},
            'config_sha256': hashlib.sha256(configuration).hexdigest(),
            'created': time.time(), 'map_before': status['map_name'], 'task_count': len(tasks)}


def stop(records):
    for record in records:
        if same(record):
            os.kill(record['pid'], signal.SIGTERM)
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and any(same(p) for p in records):
        time.sleep(.1)
    for record in records:
        if same(record):
            os.kill(record['pid'], signal.SIGKILL)
    time.sleep(.3)


def spawn(args, cwd, env, log):
    with open(STATE / log, 'ab') as output:
        child = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=output, start_new_session=True)
    return child


def wait_api(url, child):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError('Web process exited: ' + str(child.returncode))
        try:
            return get(url, 1)
        except Exception:
            time.sleep(.25)
    raise RuntimeError('Web readiness timed out')


def apply(plan):
    if time.time() - plan['created'] > 600:
        raise RuntimeError('reload plan expired')
    if not all(same(p) for p in [plan['controller']] + plan['core'] + plan['listeners']):
        raise RuntimeError('process identity changed; prepare a new plan')
    current = prepare()
    if current['config_sha256'] != plan['config_sha256']:
        raise RuntimeError('startup configuration changed')
    if {p['pid'] for p in current['children']} != {p['pid'] for p in plan['children']}:
        raise RuntimeError('navigation children changed; prepare a new plan')
    env = dict(os.environ, **plan['environment'])
    status_file = STATE / 'task-reload-20261010.json'
    state = {'phase': 'starting', 'plan': plan, 'motions_submitted': 0}
    def report():
        status_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    report()
    # Stop the manager before its children, so its watchdog cannot recreate them.
    stop(plan['listeners'])
    stop([plan['controller']])
    stop(plan['children'])
    if not all(same(p) for p in plan['core']):
        state.update(phase='failed', error='ROS core changed; no new service started')
        report()
        return
    child, backup = None, None
    command = ['python3', CONTROLLER, '__name:=x_nav_control']
    try:
        installed = subprocess.run(['python3', str(ROOT / 'navigation_task/deploy.py'), 'install'],
                                   cwd=str(ROOT), capture_output=True, text=True, timeout=10)
        if installed.returncode:
            raise RuntimeError(installed.stdout + installed.stderr)
        state['installation'] = json.loads(installed.stdout)
        backup = state['installation']['backup']
        report()
        child = spawn(command, '/root/.ros', env, 'task-web-20261010.log')
        voice = wait_api('http://127.0.0.1:9000/api/task/voice_status', child)
        listener = spawn(['python3', '-m', 'arrival_voice.legacy_auto', '--database',
                          str(STATE / 'legacy.sqlite3'), '--log', str(STATE / 'legacy.jsonl'),
                          '--allow-legacy-auto', '--allow-robot-audio'], str(ROOT), env, 'task-listener-20261010.log')
        (STATE / 'listener.pid').write_text(str(listener.pid))
        (STATE / 'web.pid').write_text(str(child.pid))
        state.update(phase='ready', web_pid=child.pid, listener_pid=listener.pid, voice_status=voice,
                     requires_map_reentry=True)
        report()
    except Exception as error:
        state.update(phase='recovering', error=str(error))
        report()
        if child and child.poll() is None:
            listing = processes()
            stop([process(child.pid)])
            stop(descendants(child.pid, listing))
        if backup:
            rolled_back = subprocess.run(['python3', str(ROOT / 'navigation_task/deploy.py'), 'rollback', '--backup', backup],
                                         capture_output=True, text=True, timeout=10)
            state['rollback'] = {'returncode': rolled_back.returncode, 'output': rolled_back.stdout + rolled_back.stderr}
            if rolled_back.returncode:
                state['phase'] = 'failed_rollback'; report(); return
        recovered = spawn(command, '/root/.ros', env, 'taREDACTED_CREDENTIAL.log')
        try:
            wait_api('http://127.0.0.1:9000/api/extra/get_control_status', recovered)
            state.update(phase='recovered_original', web_pid=recovered.pid)
        except Exception as recovery_error:
            state.update(phase='failed_recovery', recovery_error=str(recovery_error))
        report()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['prepare', 'apply'])
    parser.add_argument('--plan', required=True)
    args = parser.parse_args()
    if args.operation == 'prepare':
        plan = prepare()
        Path(args.plan).write_text(json.dumps(plan, indent=2))
        print(json.dumps(plan))
    else:
        apply(json.loads(Path(args.plan).read_text()))
